"""Команды /status, /pause и /resume."""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog
from aiogram import Router
from aiogram.filters import Command

from app.bot import texts
from app.domain.enums import NewsStatus
from app.pipeline.queue_limit import QUEUE_STATUSES

log = structlog.get_logger(__name__)

PAUSED_SETTING = "paused"
MONEY = Decimal("0.01")
TIME_FORMAT = "%d.%m.%Y %H:%M"


class StatusNewsRepo(Protocol):
    async def count_by_status(self) -> dict[NewsStatus, int]:
        """Число новостей по статусам."""
        raise NotImplementedError

    async def queue_counts_by_source(self, statuses: Sequence[NewsStatus]) -> dict[int, int]:
        """Размер очереди по источникам."""
        raise NotImplementedError


class ErrorCounted(Protocol):
    error_count: int


class NamedSource(Protocol):
    name: str


class StatusSourceRepo(Protocol):
    async def list_enabled(self, type: Any = None) -> Sequence[ErrorCounted]:
        """Включённые источники."""
        raise NotImplementedError

    async def get(self, source_id: int) -> NamedSource | None:
        """Источник по id."""
        raise NotImplementedError


class StatusCostRepo(Protocol):
    async def cost_since(self, dt: datetime) -> Decimal:
        """Расход на модель с момента dt (AiRequestRepository подходит структурно)."""
        raise NotImplementedError


class StatusPublicationRepo(Protocol):
    async def last_published_at(self, channel_id: int) -> datetime | None:
        """Время последней публикации."""
        raise NotImplementedError


class StatusSettingsRepo(Protocol):
    async def get(self, key: str, default: Any = None) -> Any:
        """Значение настройки."""
        raise NotImplementedError

    async def set(self, key: str, value: Any) -> None:
        """Запись настройки."""
        raise NotImplementedError


class StatusIncidentRepo(Protocol):
    """Нужная /status часть IncidentRepository."""

    async def count(self, open_only: bool) -> int:
        """Число инцидентов журнала."""
        raise NotImplementedError


@dataclass(frozen=True)
class StatusDeps:
    """Зависимости команд состояния (передаются в workflow_data как status_deps)."""

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    news_repo: Callable[[Any], StatusNewsRepo]
    source_repo: Callable[[Any], StatusSourceRepo]
    cost_repo: Callable[[Any], StatusCostRepo]
    publication_repo: Callable[[Any], StatusPublicationRepo]
    settings_repo: Callable[[Any], StatusSettingsRepo]
    incident_repo: Callable[[Any], StatusIncidentRepo]
    max_queued_per_source: int
    daily_budget: Decimal
    channel_id: int
    tz: ZoneInfo
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def _money(value: Decimal) -> str:
    return f"{value.quantize(MONEY, rounding=ROUND_HALF_UP)} $"


async def build_status(deps: StatusDeps) -> str:
    """Текст /status: статусы новостей, проблемные источники, расход, пауза, публикация."""
    now = deps.clock()
    day_start = datetime.combine(now.astimezone(deps.tz).date(), time.min, tzinfo=deps.tz)
    async with deps.session_scope() as session:
        counts = await deps.news_repo(session).count_by_status()
        sources = await deps.source_repo(session).list_enabled()
        spent = await deps.cost_repo(session).cost_since(day_start)
        last = await deps.publication_repo(session).last_published_at(deps.channel_id)
        paused = bool(await deps.settings_repo(session).get(PAUSED_SETTING, False))
        open_errors = await deps.incident_repo(session).count(open_only=True)
        full = await _full_sources(session, deps)

    nonzero = [f"{status.value} — {count}" for status, count in counts.items() if count]
    problems = sum(1 for source in sources if source.error_count > 0)
    last_text = last.astimezone(deps.tz).strftime(TIME_FORMAT) if last else texts.NEVER
    return "\n".join(
        [
            "<b>Состояние</b>",
            f"Пауза: {'да' if paused else 'нет'}",
            f"Новости по статусам: {', '.join(nonzero) if nonzero else texts.NEVER}",
            f"Проблемных источников: {problems}",
            f"Расход DeepSeek за сутки: {_money(spent)} из {_money(deps.daily_budget)}",
            f"Последняя публикация: {last_text}",
            texts.STATUS_ERRORS.format(count=open_errors)
            if open_errors
            else texts.STATUS_NO_ERRORS,
            *([texts.STATUS_QUEUE_FULL.format(sources=full)] if full else []),
        ]
    )


async def _full_sources(session: Any, deps: StatusDeps) -> str:
    """Источники, у которых очередь заполнена до лимита, строкой «Имя 4/4»."""
    limit = deps.max_queued_per_source
    if limit <= 0:
        return ""
    counts = await deps.news_repo(session).queue_counts_by_source(QUEUE_STATUSES)
    source_repo = deps.source_repo(session)
    full: list[str] = []
    for source_id, used in sorted(counts.items()):
        if used < limit:
            continue
        source = await source_repo.get(source_id)
        name = source.name if source is not None else f"источник #{source_id}"
        full.append(f"{html.escape(name)} {used}/{limit}")
    return ", ".join(full)


async def _set_paused(message: Any, deps: StatusDeps, paused: bool) -> None:
    async with deps.session_scope() as session:
        settings = deps.settings_repo(session)
        current = bool(await settings.get(PAUSED_SETTING, False))
        if current == paused:
            await message.answer(texts.ALREADY_PAUSED if paused else texts.NOT_PAUSED)
            return
        await settings.set(PAUSED_SETTING, paused)
    user_id = message.from_user.id if message.from_user else None
    log.info("bot.paused" if paused else "bot.resumed", user_id=user_id)
    await message.answer(texts.PAUSED if paused else texts.RESUMED)


async def cmd_status(message: Any, status_deps: StatusDeps) -> None:
    """Показывает состояние пайплайна."""
    await message.answer(await build_status(status_deps))


async def cmd_pause(message: Any, status_deps: StatusDeps) -> None:
    """Ставит пайплайн на паузу."""
    await _set_paused(message, status_deps, paused=True)


async def cmd_resume(message: Any, status_deps: StatusDeps) -> None:
    """Снимает паузу."""
    await _set_paused(message, status_deps, paused=False)


def create_router() -> Router:
    """Роутер команд состояния."""
    router = Router(name="status")
    router.message.register(cmd_status, Command("status"))
    router.message.register(cmd_pause, Command("pause"))
    router.message.register(cmd_resume, Command("resume"))
    return router
