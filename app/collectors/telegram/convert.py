"""Преобразование сообщения Telegram-канала в RawItem."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from app.collectors.base import PermanentCollectorError, RawItem
from app.db.models import Source

log = structlog.get_logger(__name__)

TME_URL = "https://t.me/{username}/{message_id}"
UTF16 = "utf-16-le"
UTF16_UNIT_BYTES = 2
DEFAULT_SCHEME = "https://"


def utf16_slice(text: str, offset: int, length: int) -> str:
    """Вырезает подстроку по смещениям в единицах UTF-16, как считает Telegram."""
    encoded = text.encode(UTF16)
    start = offset * UTF16_UNIT_BYTES
    end = (offset + length) * UTF16_UNIT_BYTES
    return encoded[start:end].decode(UTF16, errors="ignore")


def extract_links(text: str, entities: list[Any] | None) -> list[str]:
    """Ссылки из сущностей MessageEntityTextUrl и MessageEntityUrl без повторов."""
    links: list[str] = []
    for entity in entities or []:
        if isinstance(entity, MessageEntityTextUrl):
            link = str(entity.url)
        elif isinstance(entity, MessageEntityUrl):
            link = utf16_slice(text, entity.offset, entity.length).strip()
            if link and "://" not in link:
                link = DEFAULT_SCHEME + link
        else:
            continue
        if link and link not in links:
            links.append(link)
    return links


def make_title(text: str, max_chars: int) -> str:
    """Первая непустая строка текста, обрезанная до max_chars символов."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:max_chars].rstrip()
    return ""


def _channel_id(message: Any) -> int:
    peer = getattr(message, "peer_id", None)
    channel_id = getattr(peer, "channel_id", None)
    if channel_id is None:
        channel_id = getattr(message, "chat_id", None)
    if channel_id is None:
        raise PermanentCollectorError(f"у сообщения {message.id} нет id канала")
    return int(channel_id)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def message_to_raw_item(
    message: Any,
    source: Source,
    channel_username: str | None,
    title_max_chars: int,
) -> RawItem | None:
    """Превращает сообщение канала в сырую новость.

    Args:
        message: Сообщение Telethon (или объект с теми же полями).
        source: Источник-канал.
        channel_username: Username канала без @.
        title_max_chars: Максимальная длина заголовка (telegram.title_max_chars).

    Returns:
        RawItem или None, если в сообщении нет ни текста, ни фото.

    Raises:
        PermanentCollectorError: У канала нет username (читаем только публичные каналы).
    """
    if not channel_username:
        raise PermanentCollectorError(
            f"у канала источника {source.id} нет username: поддерживаются только публичные каналы"
        )
    text = str(getattr(message, "message", "") or "")
    has_photo = getattr(message, "photo", None) is not None
    if not text.strip() and not has_photo:
        return None

    raw_meta: dict[str, Any] = {
        "msg_id": int(message.id),
        "channel_id": _channel_id(message),
        "has_photo": has_photo,
        "links": extract_links(text, getattr(message, "entities", None)),
    }
    grouped_id = getattr(message, "grouped_id", None)
    if grouped_id is not None:
        raw_meta["grouped_id"] = int(grouped_id)

    username = channel_username.lstrip("@")
    return RawItem(
        source_id=source.id,
        external_id=f"{raw_meta['channel_id']}:{message.id}",
        url=TME_URL.format(username=username, message_id=message.id),
        title=make_title(text, title_max_chars),
        text=text,
        published_at=_utc(getattr(message, "date", None)),
        raw_meta=raw_meta,
    )
