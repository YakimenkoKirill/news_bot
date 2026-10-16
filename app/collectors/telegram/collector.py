"""Сборщик Telegram-каналов: догрузка пропущенного и подписка на новые сообщения."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog
from telethon import events

from app.collectors.base import RawItem
from app.collectors.telegram.ads import AdRules, is_advertisement
from app.collectors.telegram.albums import AlbumBuffer, Sleep, merge_album
from app.collectors.telegram.client import connect_authorized, with_flood_wait
from app.collectors.telegram.convert import message_to_raw_item
from app.config import TelegramConfig
from app.db.models import Source
from app.domain.enums import SourceType
from app.logging import bind_context

log = structlog.get_logger(__name__)

TME_PREFIXES = ("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/")


class TgSourcesRepo(Protocol):
    """Нужная сборщику часть SourceRepository."""

    async def list_enabled(self, type: SourceType | None = None) -> Sequence[Source]:
        """Включённые источники."""
        raise NotImplementedError


class TgNewsRepo(Protocol):
    """Нужная сборщику часть NewsRepository."""

    async def add_if_new(self, raw: RawItem) -> object | None:
        """Сохраняет новость, если её ещё нет."""
        raise NotImplementedError

    async def max_tg_message_id(self, source_id: int) -> int | None:
        """Наибольший id сообщения источника в БД."""
        raise NotImplementedError


class TgHealth(Protocol):
    """Нужная сборщику часть SourceHealthTracker."""

    async def on_success(self, source: Source, items_count: int, now: datetime) -> None:
        """Успешная обработка."""
        raise NotImplementedError

    async def on_error(self, source: Source, error: BaseException) -> int:
        """Ошибка источника."""
        raise NotImplementedError


@dataclass(frozen=True)
class TelegramDeps:
    """Зависимости сборщика каналов (фабрики от сессии БД)."""

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    source_repo: Callable[[Any], TgSourcesRepo]
    news_repo: Callable[[Any], TgNewsRepo]
    health: Callable[[Any], TgHealth]


@dataclass(frozen=True)
class _Channel:
    source: Source
    username: str
    entity: Any


def channel_username(source: Source) -> str:
    """Username канала из url источника (https://t.me/name, @name или name)."""
    value = source.url.strip()
    for prefix in TME_PREFIXES:
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    return value.lstrip("@").split("/")[0].split("?")[0]


def _chat_id(message: Any) -> int | None:
    peer = getattr(message, "peer_id", None)
    channel_id = getattr(peer, "channel_id", None)
    return channel_id if channel_id is not None else getattr(message, "chat_id", None)


def _message_date(message: Any) -> datetime | None:
    value: datetime | None = getattr(message, "date", None)
    if value is None:
        log.warning("telegram.message_without_date", msg_id=getattr(message, "id", None))
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _is_older(message: Any, cutoff: datetime) -> bool:
    """Сообщение старше границы; без даты считается свежим (как при сохранении)."""
    date = _message_date(message)
    return date is not None and date < cutoff


def _group_messages(messages: Sequence[Any]) -> list[list[Any]]:
    """Собирает сообщения в альбомы по grouped_id, по возрастанию id."""
    groups: dict[Any, list[Any]] = {}
    for message in sorted(messages, key=lambda m: int(m.id)):
        grouped_id = getattr(message, "grouped_id", None)
        key = ("album", grouped_id) if grouped_id is not None else ("single", message.id)
        groups.setdefault(key, []).append(message)
    return list(groups.values())


class TelegramCollector:
    """Компонент: подписка на каналы-источники и догрузка пропущенного при старте.

    Поток сообщения: AlbumBuffer -> отсев рекламы (по всем частям альбома) ->
    message_to_raw_item -> add_if_new + SourceHealthTracker.

    Догрузка не уходит глубже max_age_h часов: такие сообщения всё равно отсеял бы
    фильтр too_old, а в БД они занимали бы место. Новые сообщения в реальном
    времени этой границы не знают.
    """

    name = "telegram_collector"

    def __init__(
        self,
        client: Any,
        deps: TelegramDeps,
        config: TelegramConfig,
        max_age_h: float,
        *,
        clock: Callable[[], datetime] | None = None,
        album_sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = client
        self._deps = deps
        self._config = config
        self._max_age = timedelta(hours=max_age_h)
        self._rules = AdRules.from_config(config)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._albums = AlbumBuffer(config.album_wait_s, self._on_ready, sleep=album_sleep)
        self._channels: dict[int, _Channel] = {}
        self._handler_registered = False

    @property
    def channels(self) -> dict[int, Source]:
        """Подписанные каналы: id канала -> источник."""
        return {chat_id: channel.source for chat_id, channel in self._channels.items()}

    async def start(self) -> None:
        """Подключается по готовой сессии, догружает пропущенное и подписывается."""
        await connect_authorized(self._client)
        await self.reload_sources()

    async def stop(self) -> None:
        """Отдаёт накопленные альбомы, снимает обработчик и отключает клиент."""
        await self._albums.flush_all()
        self._unregister()
        await self._client.disconnect()

    async def reload_sources(self) -> None:
        """Перечитывает источники tg, догружает пропущенное и перерегистрирует обработчик."""
        self._unregister()
        async with self._deps.session_scope() as session:
            sources = list(await self._deps.source_repo(session).list_enabled(SourceType.TG))
        self._channels = await self._resolve(sources)
        await self.catch_up()
        if self._channels:
            entities = [channel.entity for channel in self._channels.values()]
            self._client.add_event_handler(self._on_new_message, events.NewMessage(chats=entities))
            self._handler_registered = True
        log.info("telegram.subscribed", channels=len(self._channels))

    async def catch_up(self) -> None:
        """Догружает сообщения каналов новее сохранённых (не больше catch_up_limit)."""
        for channel in list(self._channels.values()):
            with bind_context(source_id=channel.source.id):
                try:
                    await self._catch_up_channel(channel)
                except Exception as exc:
                    log.warning("telegram.catch_up_failed", error=repr(exc))
                    await self._register_error(channel.source, exc)

    async def _catch_up_channel(self, channel: _Channel) -> None:
        async with self._deps.session_scope() as session:
            min_id = await self._deps.news_repo(session).max_tg_message_id(channel.source.id)
        cutoff = self._clock() - self._max_age

        async def fetch() -> tuple[list[Any], bool]:
            """Читает сообщения от новых к старым до первого старше cutoff.

            Старая часть альбома, у которого уже собрана более новая часть, берётся
            тоже: решение по дате принимается по самому новому сообщению альбома.

            Returns:
                Собранные сообщения и признак остановки по дате.
            """
            collected: list[Any] = []
            albums: set[Any] = set()
            async for message in self._client.iter_messages(
                channel.entity, min_id=min_id or 0, limit=self._config.catch_up_limit
            ):
                grouped_id = getattr(message, "grouped_id", None)
                if _is_older(message, cutoff) and grouped_id not in albums:
                    return collected, True
                if grouped_id is not None:
                    albums.add(grouped_id)
                collected.append(message)
            return collected, False

        messages, stopped_by_age = await with_flood_wait(fetch, self._config)
        added = 0
        for parts in _group_messages(messages):
            added += await self._store_parts(channel, parts)
        async with self._deps.session_scope() as session:
            await self._deps.health(session).on_success(channel.source, added, self._clock())
        log.info(
            "telegram.caught_up",
            fetched=len(messages),
            new_items=added,
            min_id=min_id,
            stopped_by_age=stopped_by_age,
        )

    async def _resolve(self, sources: Sequence[Source]) -> dict[int, _Channel]:
        channels: dict[int, _Channel] = {}
        for source in sources:
            username = channel_username(source)

            async def get_entity(name: str = username) -> Any:
                return await self._client.get_entity(name)

            try:
                entity = await with_flood_wait(get_entity, self._config)
            except Exception as exc:
                log.warning("telegram.resolve_failed", source_id=source.id, error=repr(exc))
                await self._register_error(source, exc)
                continue
            channels[int(entity.id)] = _Channel(source=source, username=username, entity=entity)
        return channels

    async def _on_new_message(self, event: Any) -> None:
        await self._albums.add(event.message)

    async def _on_ready(self, parts: list[Any]) -> None:
        channel = self._channels.get(_chat_id(parts[0]) or 0)
        if channel is None:
            log.info("telegram.message_from_unknown_chat", chat_id=_chat_id(parts[0]))
            return
        with bind_context(source_id=channel.source.id):
            try:
                added = await self._store_parts(channel, parts)
            except Exception as exc:
                log.warning("telegram.message_failed", error=repr(exc))
                await self._register_error(channel.source, exc)
                return
            async with self._deps.session_scope() as session:
                await self._deps.health(session).on_success(channel.source, added, self._clock())

    async def _store_parts(self, channel: _Channel, parts: list[Any]) -> int:
        """Сохраняет сообщение или альбом; возвращает 1, если новость новая, иначе 0."""
        for part in parts:
            reason = is_advertisement(part, self._rules)
            if reason is not None:
                log.info("telegram.skipped", reason=reason, msg_id=part.id)
                return 0
        grouped = getattr(parts[0], "grouped_id", None) is not None
        message = merge_album(parts) if grouped else parts[0]
        raw = message_to_raw_item(
            message, channel.source, channel.username, self._config.title_max_chars
        )
        if raw is None:
            return 0
        async with self._deps.session_scope() as session:
            created = await self._deps.news_repo(session).add_if_new(raw)
        return 1 if created is not None else 0

    async def _register_error(self, source: Source, error: BaseException) -> None:
        async with self._deps.session_scope() as session:
            await self._deps.health(session).on_error(source, error)

    def _unregister(self) -> None:
        if self._handler_registered:
            self._client.remove_event_handler(self._on_new_message)
            self._handler_registered = False
