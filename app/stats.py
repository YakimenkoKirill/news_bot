"""Сбор статистики опубликованных постов: просмотры, пересылки, реакции."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import structlog

from app.collectors.telegram.collector import PostStats
from app.config import StatsConfig
from app.db.repositories.posts import PendingStats
from app.domain.alerts import AlertEvent, AlertKind, AlertSink

log = structlog.get_logger(__name__)

STATS_LIMIT = 100


class StatsPublicationRepo(Protocol):
    """Нужная задаче часть PublicationRepository."""

    async def list_for_stats(
        self, channel_id: int, since: datetime, until: datetime, limit: int
    ) -> list[PendingStats]:
        """Публикации, которым пора собрать статистику."""
        raise NotImplementedError

    async def save_stats(
        self,
        publication_id: int,
        at: datetime,
        *,
        views: int | None = None,
        forwards: int | None = None,
        reactions: int | None = None,
    ) -> bool:
        """Записывает статистику поста."""
        raise NotImplementedError

    async def give_up_stats(self, channel_id: int, before: datetime, at: datetime) -> int:
        """Отмечает публикации, которые уже поздно опрашивать."""
        raise NotImplementedError


class StatsFetcher(Protocol):
    """Сбор статистики из Telegram (TelegramCollector подходит структурно)."""

    async def fetch_post_stats(
        self, channel_id: int, message_ids: Sequence[int]
    ) -> dict[int, PostStats]:
        """Статистика постов канала по id сообщений."""
        raise NotImplementedError


@dataclass(frozen=True)
class StatsDeps:
    """Зависимости задачи сбора статистики.

    Attributes:
        session_scope: Открывает транзакцию БД.
        publication_repo: Репозиторий публикаций для сессии.
        fetcher: Сбор статистики из Telegram.
        sink: Получатель алертов.
        cfg: Секция stats.
        channel_id: Канал публикации.
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    publication_repo: Callable[[Any], StatsPublicationRepo]
    fetcher: StatsFetcher
    sink: AlertSink
    cfg: StatsConfig
    channel_id: int


@dataclass(frozen=True)
class StatsReport:
    """Итог одного запуска задачи.

    Attributes:
        requested: Сколько публикаций опрашивали.
        updated: У скольких статистика записана.
        missing: Сколько постов не нашлось в канале (удалены).
        given_up: Сколько старых публикаций отмечены без статистики.
        failed: True, если канал недоступен аккаунту Telethon.
    """

    requested: int = 0
    updated: int = 0
    missing: int = 0
    given_up: int = 0
    failed: bool = False


async def collect_post_stats(now: datetime, deps: StatsDeps) -> StatsReport:
    """Собирает статистику постов, вышедших не позже stats.delay_h назад.

    Посты старше stats.max_age_h получают stats_at без значений: Telegram их
    статистику уже не отдаст, а запрашивать вечно не нужно. Недоступный аккаунту
    канал не роняет задачу: уходит инцидент stats_unavailable (в чат он не
    приходит, если вид в alerts.silent_kinds).

    Args:
        now: Текущий момент.
        deps: Зависимости.

    Returns:
        Отчёт о запуске.
    """
    since = now - timedelta(hours=deps.cfg.max_age_h)
    until = now - timedelta(hours=deps.cfg.delay_h)
    async with deps.session_scope() as session:
        pending = await deps.publication_repo(session).list_for_stats(
            deps.channel_id, since, until, STATS_LIMIT
        )
        given_up = await deps.publication_repo(session).give_up_stats(deps.channel_id, since, now)
    if not pending:
        if given_up:
            log.info("stats.given_up", count=given_up)
        return StatsReport(given_up=given_up)

    by_message = {item.message_id: item for item in pending}
    try:
        collected = await deps.fetcher.fetch_post_stats(deps.channel_id, list(by_message))
    except Exception as exc:
        log.warning("stats.channel_unavailable", channel_id=deps.channel_id, error=repr(exc))
        await _alert_unavailable(exc, now, deps)
        return StatsReport(requested=len(pending), given_up=given_up, failed=True)

    updated = 0
    async with deps.session_scope() as session:
        repo = deps.publication_repo(session)
        for message_id, item in by_message.items():
            stats = collected.get(message_id)
            if stats is None:
                await repo.save_stats(item.publication_id, now)
                continue
            await repo.save_stats(
                item.publication_id,
                now,
                views=stats.views,
                forwards=stats.forwards,
                reactions=stats.reactions,
            )
            updated += 1
    report = StatsReport(
        requested=len(pending),
        updated=updated,
        missing=len(pending) - updated,
        given_up=given_up,
    )
    log.info(
        "stats.collected",
        requested=report.requested,
        updated=report.updated,
        missing=report.missing,
        given_up=report.given_up,
    )
    return report


async def _alert_unavailable(error: Exception, now: datetime, deps: StatsDeps) -> None:
    await deps.sink.emit(
        AlertEvent(
            kind=AlertKind.STATS_UNAVAILABLE,
            key=f"{AlertKind.STATS_UNAVAILABLE.value}:{deps.channel_id}",
            message=(
                f"Не удалось прочитать статистику канала {deps.channel_id}: {error}. "
                "Аккаунт Telethon должен быть участником или администратором канала."
            ),
            created_at=now,
        )
    )
