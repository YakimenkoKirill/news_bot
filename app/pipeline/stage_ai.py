"""Стадия ИИ-обработки нормализованных новостей."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any, Protocol

import structlog

from app.db.models import ProcessedPost
from app.domain.alerts import AlertEvent, AlertKind, AlertSink
from app.domain.enums import NewsStatus
from app.logging import bind_context
from app.pipeline.ai.budget import BudgetExceeded
from app.pipeline.ai.client import AiFatalError, AiUnavailableError
from app.pipeline.ai.processor import (
    REASON_BAD_AI_RESPONSE,
    AiRepos,
    ModeratedSource,
    PostRepo,
    ProcessableNews,
    StatusRepo,
)
from app.pipeline.queue_limit import QueueAdmission

log = structlog.get_logger(__name__)

PAUSED_SETTING = "paused"


class ClaimableNews(ProcessableNews, Protocol):
    """Захваченная новость (NewsItem подходит структурно)."""

    source_id: int


class AiNewsRepo(StatusRepo, Protocol):
    """Нужная стадии часть NewsRepository."""

    async def claim_batch(self, status: NewsStatus, limit: int) -> Sequence[ClaimableNews]:
        """Захватывает пачку новостей."""
        raise NotImplementedError


class SourceGetter(Protocol):
    """Нужная стадии часть SourceRepository."""

    async def get(self, source_id: int) -> ModeratedSource | None:
        """Источник по id."""
        raise NotImplementedError


class AiPostRepo(PostRepo, Protocol):
    """Нужная стадии часть ProcessedPostRepository."""

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        """Пересказ новости."""
        raise NotImplementedError


class SettingsGetter(Protocol):
    """Нужная стадии часть SettingsRepository."""

    async def get(self, key: str, default: Any = None) -> Any:
        """Значение настройки."""
        raise NotImplementedError


class NewsProcessor(Protocol):
    """Обработчик одной новости (AiProcessor подходит структурно)."""

    async def process(self, news: ProcessableNews, source: ModeratedSource) -> NewsStatus:
        """Обрабатывает новость и возвращает итоговый статус."""
        raise NotImplementedError


class BudgetChecker(Protocol):
    """Проверка дневного бюджета (BudgetGuard подходит структурно)."""

    async def check(self, now: datetime) -> Any:
        """Бросает BudgetExceeded, если бюджет исчерпан."""
        raise NotImplementedError


class ReviewNotifier(Protocol):
    """Отправка поста администраторам на модерацию (реализация — в боте)."""

    async def notify(self, post: ProcessedPost) -> None:
        """Уведомляет администраторов о посте на модерации."""
        raise NotImplementedError


@dataclass(frozen=True)
class AiStageDeps:
    """Зависимости стадии ИИ-обработки.

    Attributes:
        session_scope: Открывает транзакцию БД.
        news_repo: Репозиторий новостей для сессии.
        source_repo: Репозиторий источников для сессии.
        post_repo: Репозиторий пересказов для сессии.
        settings_repo: Репозиторий настроек для сессии.
        processor: Создаёт обработчик новости для репозиториев одной сессии.
        budget: Проверка дневного бюджета.
        notifier: Уведомление о постах на модерации.
        sink: Получатель алертов.
        batch_size: Размер пачки (pipeline.batch_size).
        max_concurrency: Сколько новостей обрабатывать одновременно (ai.max_concurrency).
        admission: Лимит источника при постановке в очередь; None — без лимита.
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    news_repo: Callable[[Any], AiNewsRepo]
    source_repo: Callable[[Any], SourceGetter]
    post_repo: Callable[[Any], AiPostRepo]
    settings_repo: Callable[[Any], SettingsGetter]
    processor: Callable[[AiRepos], NewsProcessor]
    budget: BudgetChecker
    notifier: ReviewNotifier
    sink: AlertSink
    batch_size: int
    max_concurrency: int
    admission: QueueAdmission | None = None


@dataclass(frozen=True)
class AiStageReport:
    """Итоги стадии.

    Attributes:
        claimed: Сколько новостей взято в пачку.
        statuses: Итоговые статусы обработанных новостей.
        errors: Новости с ошибкой (остались normalized).
        skipped: Не обработаны из-за остановки пачки (остались normalized).
        stopped: Причина остановки: paused, budget, ai_down или None.
    """

    claimed: int = 0
    statuses: dict[NewsStatus, int] = field(default_factory=dict)
    errors: int = 0
    skipped: int = 0
    stopped: str | None = None


class _StopBatch(Exception):
    def __init__(self, reason: str, error: Exception | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error = error


async def process_normalized_batch(now: datetime, deps: AiStageDeps) -> AiStageReport:
    """Обрабатывает пачку новостей в статусе normalized моделью.

    Пауза (настройка paused) и бюджет проверяются до захвата пачки; бюджет — ещё
    и перед каждой новостью. Пачка захватывается короткой транзакцией, каждая
    новость обрабатывается в своей транзакции (смена статуса защищена ожидаемым
    статусом), не больше max_concurrency одновременно. BudgetExceeded и ошибки API
    (недоступен или отклоняет запросы) останавливают пачку: необработанные новости
    остаются normalized, для ошибок API отправляется алерт ai_down.

    Args:
        now: Текущий момент.
        deps: Зависимости.

    Returns:
        Отчёт по пачке.
    """
    async with deps.session_scope() as session:
        if await deps.settings_repo(session).get(PAUSED_SETTING, False):
            log.info("ai_stage.paused")
            return AiStageReport(stopped="paused")
    try:
        await deps.budget.check(now)
    except BudgetExceeded:
        return AiStageReport(stopped="budget")

    async with deps.session_scope() as session:
        news_repo = deps.news_repo(session)
        batch = list(await news_repo.claim_batch(NewsStatus.NORMALIZED, deps.batch_size))
    if not batch:
        return AiStageReport()

    semaphore = asyncio.Semaphore(deps.max_concurrency)
    statuses: Counter[NewsStatus] = Counter()
    counters = {"errors": 0, "skipped": 0}
    stop: list[str] = []

    async def run(item: ClaimableNews) -> None:
        async with semaphore:
            if stop:
                counters["skipped"] += 1
                return
            with bind_context(news_id=item.id):
                try:
                    status = await _process_one(item, now, deps)
                except _StopBatch as exc:
                    if not stop:
                        stop.append(exc.reason)
                        if exc.error is not None:
                            await _alert_ai_down(exc.error, now, deps)
                    counters["skipped"] += 1
                    return
                except Exception as exc:
                    log.exception("ai_stage.item_failed")
                    counters["errors"] += 1
                    await _alert_item_failed(item, repr(exc), now, deps)
                    return
                if status is not None:
                    statuses[status] += 1

    await asyncio.gather(*(run(item) for item in batch))
    report = AiStageReport(
        claimed=len(batch),
        statuses=dict(statuses),
        errors=counters["errors"],
        skipped=counters["skipped"],
        stopped=stop[0] if stop else None,
    )
    log.info(
        "ai_stage.batch_done",
        claimed=report.claimed,
        statuses={s.value: c for s, c in report.statuses.items()},
        errors=report.errors,
        skipped=report.skipped,
        stopped=report.stopped,
    )
    return report


async def _process_one(item: ClaimableNews, now: datetime, deps: AiStageDeps) -> NewsStatus | None:
    try:
        await deps.budget.check(now)
    except BudgetExceeded as exc:
        raise _StopBatch("budget") from exc

    post: ProcessedPost | None = None
    try:
        async with deps.session_scope() as session:
            source = await deps.source_repo(session).get(item.source_id)
            if source is None:
                log.warning("ai_stage.source_missing", source_id=item.source_id)
                return None
            posts = deps.post_repo(session)
            admit = (
                partial(deps.admission.admit, session, expected=NewsStatus.PROCESSED)
                if deps.admission is not None
                else None
            )
            processor = deps.processor(
                AiRepos(news=deps.news_repo(session), posts=posts, admit=admit)
            )
            status = await processor.process(item, source)
            if status is NewsStatus.PENDING_REVIEW:
                post = await posts.get_by_news(item.id)
    except (AiUnavailableError, AiFatalError) as exc:
        raise _StopBatch("ai_down", exc) from exc

    if status is NewsStatus.FAILED:
        await _alert_item_failed(item, REASON_BAD_AI_RESPONSE, now, deps)
    if post is not None:
        try:
            await deps.notifier.notify(post)
        except Exception:
            log.exception("ai_stage.notify_failed", post_id=post.id)
    return status


async def _alert_item_failed(
    item: ClaimableNews, reason: str, now: datetime, deps: AiStageDeps
) -> None:
    """Сообщает об ошибке обработки одной новости; ошибка отправки не ломает пачку."""
    try:
        await deps.sink.emit(
            AlertEvent(
                kind=AlertKind.AI_ITEM_FAILED,
                key=f"{AlertKind.AI_ITEM_FAILED.value}:{item.id}",
                message=f"Новость «{item.title}» (id {item.id}) не обработана: {reason}",
                created_at=now,
            )
        )
    except Exception:
        log.exception("ai_stage.alert_failed", news_id=item.id)


async def _alert_ai_down(error: Exception, now: datetime, deps: AiStageDeps) -> None:
    log.error("ai_stage.ai_down", error=repr(error))
    await deps.sink.emit(
        AlertEvent(
            kind=AlertKind.AI_DOWN,
            key=f"{AlertKind.AI_DOWN.value}:{type(error).__name__}",
            message=f"Модель недоступна или отклоняет запросы: {error}. Обработка приостановлена.",
            created_at=now,
        )
    )
