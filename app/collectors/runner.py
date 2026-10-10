"""Цикл сбора новостей из RSS и сайтов по расписанию."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import structlog

from app.collectors.base import BaseCollector, FetchResult, RawItem
from app.db.models import Source
from app.domain.enums import SourceType
from app.logging import bind_context

log = structlog.get_logger(__name__)

EVENT_DRIVEN_TYPES = frozenset({SourceType.TG})


class DueSourcesRepo(Protocol):
    """Нужная циклу часть SourceRepository."""

    async def list_due(self, now: datetime) -> Sequence[Source]:
        """Источники, которые пора проверить."""
        raise NotImplementedError

    async def mark_checked(self, source_id: int, at: datetime, fetch_state: dict[str, Any]) -> bool:
        """Запоминает время проверки и состояние сборщика."""
        raise NotImplementedError


class NewsAdder(Protocol):
    """Нужная циклу часть NewsRepository."""

    async def add_if_new(self, raw: RawItem) -> object | None:
        """Сохраняет новость, если её ещё нет."""
        raise NotImplementedError


class HealthTracker(Protocol):
    """Нужная циклу часть SourceHealthTracker."""

    async def on_success(self, source: Source, items_count: int, now: datetime) -> None:
        """Успешный опрос."""
        raise NotImplementedError

    async def on_error(self, source: Source, error: BaseException) -> int:
        """Ошибка опроса."""
        raise NotImplementedError


@dataclass(frozen=True)
class CollectionDeps:
    """Зависимости цикла сбора.

    Attributes:
        session_scope: Открывает транзакцию БД (session_scope(factory)).
        source_repo: Репозиторий источников для сессии.
        news_repo: Репозиторий новостей для сессии.
        health: Трекер здоровья источников для сессии.
        collectors: Сборщики по типу источника.
        max_parallel_sources: Сколько источников опрашивать одновременно.
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    source_repo: Callable[[Any], DueSourcesRepo]
    news_repo: Callable[[Any], NewsAdder]
    health: Callable[[Any], HealthTracker]
    collectors: Mapping[SourceType, BaseCollector]
    max_parallel_sources: int


@dataclass(frozen=True)
class CycleReport:
    """Итоги одного цикла сбора."""

    checked: int = 0
    new_items: int = 0
    errors: int = 0
    skipped: int = 0


class UnknownSourceTypeError(Exception):
    """Для типа источника нет сборщика."""


async def run_collection_cycle(now: datetime, deps: CollectionDeps) -> CycleReport:
    """Опрашивает все просроченные источники RSS и сайтов.

    Источники обрабатываются параллельно (не больше max_parallel_sources), каждый —
    в своей транзакции и в контексте логов source_id. Ошибка одного источника
    засчитывается ему и не останавливает остальные. Источники tg собираются
    событиями и пропускаются.

    Args:
        now: Момент цикла (timezone-aware).
        deps: Зависимости.

    Returns:
        Число проверенных источников, новых новостей, ошибок и пропущенных.
    """
    async with deps.session_scope() as session:
        due = list(await deps.source_repo(session).list_due(now))

    to_collect = [s for s in due if s.type not in EVENT_DRIVEN_TYPES]
    skipped = len(due) - len(to_collect)
    semaphore = asyncio.Semaphore(deps.max_parallel_sources)

    async def guarded(source: Source) -> int | None:
        async with semaphore:
            return await _collect_source(source, now, deps)

    results = await asyncio.gather(*(guarded(source) for source in to_collect))
    report = CycleReport(
        checked=len(to_collect),
        new_items=sum(r for r in results if r is not None),
        errors=sum(1 for r in results if r is None),
        skipped=skipped,
    )
    log.info(
        "collection.cycle_done",
        checked=report.checked,
        new_items=report.new_items,
        errors=report.errors,
        skipped=report.skipped,
    )
    return report


async def _collect_source(source: Source, now: datetime, deps: CollectionDeps) -> int | None:
    """Опрашивает один источник; возвращает число новых новостей или None при ошибке."""
    with bind_context(source_id=source.id):
        try:
            result = await _fetch(source, deps)
        except Exception as exc:
            log.warning("collection.source_failed", error=repr(exc))
            await _register_error(source, now, exc, deps)
            return None

        async with deps.session_scope() as session:
            news = deps.news_repo(session)
            new_count = 0
            for item in result.items:
                if await news.add_if_new(item) is not None:
                    new_count += 1
            await deps.source_repo(session).mark_checked(source.id, now, result.fetch_state)
            await deps.health(session).on_success(source, new_count, now)
        log.info("collection.source_done", fetched=len(result.items), new_items=new_count)
        return new_count


async def _fetch(source: Source, deps: CollectionDeps) -> FetchResult:
    collector = deps.collectors.get(source.type)
    if collector is None:
        raise UnknownSourceTypeError(f"нет сборщика для типа {source.type!s}")
    return await collector.fetch_result(source)


async def _register_error(
    source: Source, now: datetime, error: BaseException, deps: CollectionDeps
) -> None:
    async with deps.session_scope() as session:
        await deps.health(session).on_error(source, error)
        await deps.source_repo(session).mark_checked(source.id, now, dict(source.fetch_state))
