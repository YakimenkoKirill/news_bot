from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import FloodWaitError

from app.collectors.base import TemporaryCollectorError
from app.collectors.telegram.collector import TelegramCollector, TelegramDeps
from app.domain.enums import SourceType
from app.publisher.media import TelegramPhotoFetcher
from tests.config_helpers import telegram_config

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CONFIG = telegram_config()
MAX_BYTES = 5 * 1024 * 1024
SOURCE_URL = "https://t.me/chan_a"
PHOTO = b"\xff\xd8photo-bytes"


@dataclass
class News:
    source_id: int = 1
    raw_meta: dict[str, Any] = field(default_factory=lambda: {"msg_id": 7, "has_photo": True})


@dataclass
class Source:
    type: SourceType = SourceType.TG
    url: str = SOURCE_URL


class PhotoClient:
    """Клиент Telethon: отдаёт сообщение по id и байты фото."""

    def __init__(
        self,
        message: Any = None,
        data: Any = PHOTO,
        download_error: Exception | None = None,
    ) -> None:
        self.message = message
        self.data = data
        self.download_error = download_error
        self.requests: list[tuple[Any, list[int]]] = []
        self.downloads: list[Any] = []
        self.flood: list[int] = []

    async def get_messages(self, entity: Any, ids: list[int]) -> list[Any]:
        self.requests.append((entity, list(ids)))
        if self.flood:
            raise FloodWaitError(request=None, capture=self.flood.pop(0))
        return [self.message]

    async def download_media(self, message: Any, file: Any) -> Any:
        self.downloads.append(message)
        if self.download_error is not None:
            raise self.download_error
        return self.data


def _message(photo: bool = True, message_id: int = 7) -> Any:
    return SimpleNamespace(
        id=message_id,
        photo=SimpleNamespace(id=100) if photo else None,
        document=None if photo else SimpleNamespace(id=200),
    )


def _collector(client: PhotoClient) -> TelegramCollector:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Any]:
        yield object()

    deps = TelegramDeps(
        session_scope=scope,
        source_repo=lambda session: SimpleNamespace(),
        news_repo=lambda session: SimpleNamespace(),
        health=lambda session: SimpleNamespace(),
    )
    return TelegramCollector(client, deps, CONFIG, 24.0, clock=lambda: NOW)


async def test_download_photo_returns_bytes() -> None:
    client = PhotoClient(_message())
    photo = await _collector(client).download_photo(SOURCE_URL, 7, MAX_BYTES)
    assert photo == PHOTO
    assert client.requests == [("chan_a", [7])]
    assert len(client.downloads) == 1


async def test_download_photo_accepts_bytearray() -> None:
    client = PhotoClient(_message(), data=bytearray(PHOTO))
    photo = await _collector(client).download_photo(SOURCE_URL, 7, MAX_BYTES)
    assert photo == PHOTO
    assert isinstance(photo, bytes)


async def test_album_uses_first_message_photo() -> None:
    """msg_id альбома указывает на первую часть — её фото и уходит в пост."""
    client = PhotoClient(_message(message_id=60))
    assert await _collector(client).download_photo(SOURCE_URL, 60, MAX_BYTES) == PHOTO
    assert client.requests == [("chan_a", [60])]


async def test_deleted_message_gives_none() -> None:
    client = PhotoClient(None)
    assert await _collector(client).download_photo(SOURCE_URL, 7, MAX_BYTES) is None
    assert client.downloads == []


async def test_message_without_photo_gives_none() -> None:
    """Видео или документ вместо фото: пост уходит текстом."""
    client = PhotoClient(_message(photo=False))
    assert await _collector(client).download_photo(SOURCE_URL, 7, MAX_BYTES) is None
    assert client.downloads == []


async def test_too_large_photo_gives_none() -> None:
    client = PhotoClient(_message(), data=b"x" * 11)
    assert await _collector(client).download_photo(SOURCE_URL, 7, 10) is None


async def test_photo_exactly_at_limit_is_returned() -> None:
    client = PhotoClient(_message(), data=b"x" * 10)
    assert await _collector(client).download_photo(SOURCE_URL, 7, 10) == b"x" * 10


async def test_download_returning_path_gives_none() -> None:
    client = PhotoClient(_message(), data="/tmp/photo.jpg")
    assert await _collector(client).download_photo(SOURCE_URL, 7, MAX_BYTES) is None


async def test_flood_wait_is_retried() -> None:
    client = PhotoClient(_message())
    client.flood = [1]
    assert await _collector(client).download_photo(SOURCE_URL, 7, MAX_BYTES) == PHOTO
    assert len(client.requests) == 2


async def test_long_flood_wait_raises() -> None:
    client = PhotoClient(_message())
    client.flood = [int(CONFIG.flood_wait_max_s) + 10]
    with pytest.raises(TemporaryCollectorError):
        await _collector(client).download_photo(SOURCE_URL, 7, MAX_BYTES)


async def test_network_error_propagates() -> None:
    client = PhotoClient(_message(), download_error=RuntimeError("Telethon отключён"))
    with pytest.raises(RuntimeError, match="Telethon"):
        await _collector(client).download_photo(SOURCE_URL, 7, MAX_BYTES)


def _fetcher(
    source: Source | None = None,
    photo: bytes | None = PHOTO,
    error: Exception | None = None,
) -> tuple[TelegramPhotoFetcher, list[tuple[str, int, int]], list[int]]:
    downloads: list[tuple[str, int, int]] = []
    lookups: list[int] = []

    async def download(source_url: str, msg_id: int, max_bytes: int) -> bytes | None:
        downloads.append((source_url, msg_id, max_bytes))
        if error is not None:
            raise error
        return photo

    async def lookup(source_id: int) -> Source | None:
        lookups.append(source_id)
        return source

    fetcher = TelegramPhotoFetcher(download=download, source=lookup, max_bytes=MAX_BYTES)
    return fetcher, downloads, lookups


async def test_fetcher_downloads_for_telegram_source() -> None:
    fetcher, downloads, lookups = _fetcher(Source())
    assert await fetcher.fetch(News()) == PHOTO
    assert downloads == [(SOURCE_URL, 7, MAX_BYTES)]
    assert lookups == [1]


@pytest.mark.parametrize(
    "raw_meta",
    [{}, {"msg_id": 7}, {"has_photo": False, "msg_id": 7}, {"has_photo": True}],
    ids=["empty", "no-flag", "flag-false", "no-msg-id"],
)
async def test_fetcher_skips_without_photo_flag(raw_meta: dict[str, Any]) -> None:
    fetcher, downloads, lookups = _fetcher(Source())
    assert await fetcher.fetch(News(raw_meta=raw_meta)) is None
    assert downloads == []
    assert lookups == []


@pytest.mark.parametrize("source_type", [SourceType.RSS, SourceType.WEB])
async def test_fetcher_skips_other_source_types(source_type: SourceType) -> None:
    """RSS и сайты работают как раньше — по URL картинки."""
    fetcher, downloads, _ = _fetcher(Source(type=source_type))
    assert await fetcher.fetch(News()) is None
    assert downloads == []


async def test_fetcher_without_source_gives_none() -> None:
    fetcher, downloads, _ = _fetcher(None)
    assert await fetcher.fetch(News()) is None
    assert downloads == []


async def test_fetcher_passes_through_none_from_download() -> None:
    fetcher, _, _ = _fetcher(Source(), photo=None)
    assert await fetcher.fetch(News()) is None


async def test_fetcher_propagates_download_error() -> None:
    """Решение, что делать с ошибкой, принимает публикация: пост уйдёт текстом."""
    fetcher, _, _ = _fetcher(Source(), error=RuntimeError("Telethon отключён"))
    with pytest.raises(RuntimeError):
        await fetcher.fetch(News())


def test_real_collector_fits_fetcher_protocol() -> None:
    collector = _collector(PhotoClient(_message()))

    async def lookup(source_id: int) -> Source | None:
        return Source()

    fetcher = TelegramPhotoFetcher(
        download=collector.download_photo, source=lookup, max_bytes=MAX_BYTES
    )
    assert fetcher.max_bytes == MAX_BYTES


async def test_fetcher_ignores_empty_raw_meta() -> None:
    @dataclass
    class NoMeta:
        source_id: int = 1
        raw_meta: dict[str, Any] = field(default_factory=dict)

    fetcher, downloads, _ = _fetcher(Source())
    assert await fetcher.fetch(NoMeta()) is None
    assert downloads == []
