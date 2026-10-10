"""Склейка альбомов Telegram (несколько сообщений с одним grouped_id) в одну новость."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

OnReady = Callable[[list[Any]], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]
AlbumKey = tuple[int | None, int]


@dataclass(frozen=True)
class AlbumMessage:
    """Склеенный альбом с полями сообщения, которые читает message_to_raw_item."""

    id: int
    message: str
    entities: list[Any]
    photo: Any
    date: datetime | None
    peer_id: Any
    grouped_id: int | None
    photo_ids: list[int] = field(default_factory=list)


def _chat_key(message: Any) -> int | None:
    peer = getattr(message, "peer_id", None)
    channel_id = getattr(peer, "channel_id", None)
    return channel_id if channel_id is not None else getattr(message, "chat_id", None)


def merge_album(messages: Sequence[Any]) -> AlbumMessage:
    """Склеивает части альбома.

    Текст и сущности берутся из первой части, где есть текст; id — наименьший id
    частей (одинаковый при повторной загрузке); собираются id всех фото.

    Raises:
        ValueError: Пустой список.
    """
    if not messages:
        raise ValueError("альбом без сообщений")
    ordered = sorted(messages, key=lambda m: int(m.id))
    with_text = next((m for m in ordered if str(getattr(m, "message", "") or "").strip()), None)
    photos = [m.photo for m in ordered if getattr(m, "photo", None) is not None]
    first = ordered[0]
    return AlbumMessage(
        id=int(first.id),
        message=str(with_text.message) if with_text is not None else "",
        entities=list(getattr(with_text, "entities", None) or []) if with_text else [],
        photo=photos[0] if photos else None,
        date=getattr(first, "date", None),
        peer_id=getattr(first, "peer_id", None),
        grouped_id=getattr(first, "grouped_id", None),
        photo_ids=[int(p.id) for p in photos if getattr(p, "id", None) is not None],
    )


class AlbumBuffer:
    """Копит части альбомов и отдаёт их одним списком через wait_s после последней.

    Сообщение без grouped_id передаётся в on_ready сразу.
    """

    def __init__(self, wait_s: float, on_ready: OnReady, *, sleep: Sleep = asyncio.sleep) -> None:
        self._wait_s = wait_s
        self._on_ready = on_ready
        self._sleep = sleep
        self._parts: dict[AlbumKey, list[Any]] = {}
        self._timers: dict[AlbumKey, asyncio.Task[None]] = {}

    @property
    def pending(self) -> int:
        """Число альбомов в ожидании."""
        return len(self._parts)

    async def add(self, message: Any) -> None:
        """Принимает сообщение канала."""
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is None:
            await self._on_ready([message])
            return
        key: AlbumKey = (_chat_key(message), int(grouped_id))
        self._parts.setdefault(key, []).append(message)
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        self._timers[key] = asyncio.create_task(self._release_later(key))

    async def flush_all(self) -> None:
        """Отменяет таймеры и отдаёт все накопленные альбомы (при остановке)."""
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            timer.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        while self._parts:
            key = next(iter(self._parts))
            await self._emit(key)

    async def _release_later(self, key: AlbumKey) -> None:
        await self._sleep(self._wait_s)
        self._timers.pop(key, None)
        await self._emit(key)

    async def _emit(self, key: AlbumKey) -> None:
        parts = self._parts.pop(key, [])
        if not parts:
            return
        try:
            await self._on_ready(parts)
        except Exception:
            log.exception("telegram.album_handler_failed", chat_id=key[0], grouped_id=key[1])
