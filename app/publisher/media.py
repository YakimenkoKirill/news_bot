"""Картинка поста: проверка URL и загрузка фото из Telegram-источника."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Protocol

import structlog

from app.collectors.base import PermanentCollectorError, TemporaryCollectorError
from app.collectors.http import HttpClient, HttpResponse, HttpStatusError
from app.config import PublisherConfig
from app.domain.enums import SourceType

log = structlog.get_logger(__name__)

REASON_NO_IMAGE = "no_image"
REASON_TIMEOUT = "timeout"
REASON_UNAVAILABLE = "unavailable"
REASON_NO_CONTENT_TYPE = "no_content_type"
FIRST_BYTE_RANGE = {"Range": "bytes=0-0"}
OK_STATUSES = frozenset({HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT})
_CONTENT_RANGE_TOTAL = re.compile(r"/(\d+)\s*$")


HAS_PHOTO_KEY = "has_photo"
MSG_ID_KEY = "msg_id"


@dataclass(frozen=True)
class ImageCheck:
    """Результат проверки: подходит ли картинка и почему нет."""

    ok: bool
    reason: str | None = None


class PhotoNews(Protocol):
    """Новость, у которой может быть фото в Telegram (NewsItem подходит)."""

    @property
    def source_id(self) -> int:
        """Источник новости."""
        raise NotImplementedError

    @property
    def raw_meta(self) -> Mapping[str, Any]:
        """Служебные данные сборщика: msg_id, has_photo и прочее."""
        raise NotImplementedError


class PhotoSource(Protocol):
    """Источник новости (Source подходит структурно)."""

    @property
    def type(self) -> SourceType:
        """Вид источника."""
        raise NotImplementedError

    @property
    def url(self) -> str:
        """Адрес источника."""
        raise NotImplementedError


class PhotoFetcher(Protocol):
    """Загрузка фото новости, у которой нет публичного URL картинки."""

    async def fetch(self, news: PhotoNews) -> bytes | None:
        """Байты фото или None, если фото нет."""
        raise NotImplementedError


@dataclass(frozen=True)
class TelegramPhotoFetcher:
    """Фото из исходного сообщения Telegram-канала.

    У фото в Telegram нет публичного адреса, поэтому его приходится скачивать
    через Telethon. Для источников другого вида и сообщений без фото загрузка
    не запускается.

    Attributes:
        download: TelegramCollector.download_photo.
        source: Источник новости по source_id (своя короткая транзакция).
        max_bytes: Предел размера файла (publisher.image_max_bytes).
    """

    download: Callable[[str, int, int], Awaitable[bytes | None]]
    source: Callable[[int], Awaitable[PhotoSource | None]]
    max_bytes: int

    async def fetch(self, news: PhotoNews) -> bytes | None:
        """Скачивает фото сообщения, из которого собрана новость.

        Args:
            news: Новость с raw_meta сборщика.

        Returns:
            Байты фото или None, если фото нет, источник не Telegram или файл
            слишком большой.
        """
        meta = news.raw_meta or {}
        msg_id = meta.get(MSG_ID_KEY)
        if not meta.get(HAS_PHOTO_KEY) or not isinstance(msg_id, int):
            return None
        source = await self.source(news.source_id)
        if source is None or source.type is not SourceType.TG:
            return None
        return await self.download(source.url, msg_id, self.max_bytes)


def _content_type(headers: Mapping[str, str]) -> str | None:
    value = headers.get("Content-Type")
    return value.split(";", 1)[0].strip().lower() if value else None


def _size(response: HttpResponse) -> int | None:
    """Полный размер файла: из Content-Range для 206, иначе из Content-Length."""
    if response.status == HTTPStatus.PARTIAL_CONTENT:
        match = _CONTENT_RANGE_TOTAL.search(response.headers.get("Content-Range", ""))
        if match:
            return int(match.group(1))
        return None
    length = response.headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


async def _fetch_headers(url: str, http: HttpClient) -> HttpResponse:
    """HEAD; при 405 или ответе без Content-Type — GET первого байта."""
    try:
        response = await http.request("HEAD", url, read_body=False)
        if _content_type(response.headers):
            return response
    except HttpStatusError as exc:
        if exc.status != HTTPStatus.METHOD_NOT_ALLOWED:
            raise
    return await http.request("GET", url, FIRST_BYTE_RANGE, read_body=False)


async def check_image(url: str | None, http_client: HttpClient, cfg: PublisherConfig) -> ImageCheck:
    """Проверяет, что картинку можно отправить по URL.

    Args:
        url: Адрес картинки.
        http_client: Общий HTTP-клиент.
        cfg: Секция publisher (image_types, image_max_bytes).

    Returns:
        ok=True, если статус 200/206, тип из image_types и размер не больше
        image_max_bytes (неизвестный размер допускается с предупреждением);
        иначе ok=False и причина: no_image, timeout, unavailable, http_<код>,
        no_content_type, bad_type:<тип>, too_large:<байт>.
    """
    if not url:
        return ImageCheck(ok=False, reason=REASON_NO_IMAGE)
    try:
        response = await _fetch_headers(url, http_client)
    except HttpStatusError as exc:
        return ImageCheck(ok=False, reason=f"http_{exc.status}")
    except TemporaryCollectorError as exc:
        timed_out = isinstance(exc.__cause__, asyncio.TimeoutError)
        log.info("image.unreachable", url=url, error=str(exc))
        return ImageCheck(ok=False, reason=REASON_TIMEOUT if timed_out else REASON_UNAVAILABLE)
    except PermanentCollectorError as exc:
        log.info("image.bad_url", url=url, error=str(exc))
        return ImageCheck(ok=False, reason=REASON_UNAVAILABLE)

    if response.status not in OK_STATUSES:
        return ImageCheck(ok=False, reason=f"http_{response.status}")
    content_type = _content_type(response.headers)
    if content_type is None:
        return ImageCheck(ok=False, reason=REASON_NO_CONTENT_TYPE)
    if content_type not in cfg.image_types:
        return ImageCheck(ok=False, reason=f"bad_type:{content_type}")
    size = _size(response)
    if size is None:
        log.warning("image.size_unknown", url=url)
    elif size > cfg.image_max_bytes:
        return ImageCheck(ok=False, reason=f"too_large:{size}")
    return ImageCheck(ok=True)
