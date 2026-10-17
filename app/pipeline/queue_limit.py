"""Лимит постов одного источника в очереди публикации.

В очереди (approved и scheduled) остаётся не больше max_queued_per_source постов
одного источника — самые сильные по приоритету. Лишние уходят в filtered с
причиной queue_limit.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import structlog

from app.db.repositories.news import Priority, QueuedRef
from app.domain.enums import NewsStatus
from app.runtime_config import RuntimeSource

log = structlog.get_logger(__name__)

REASON_QUEUE_LIMIT = "queue_limit"
QUEUE_STATUSES = (NewsStatus.APPROVED, NewsStatus.SCHEDULED)


class LimitNewsRepo(Protocol):
    """Нужная лимиту часть NewsRepository."""

    async def set_status(
        self,
        news_id: int,
        expected: NewsStatus,
        new: NewsStatus,
        reason: str | None = None,
    ) -> None:
        """Смена статуса через машину состояний."""
        raise NotImplementedError

    async def queued_by_source(
        self, source_id: int, statuses: Sequence[NewsStatus], priority: Priority
    ) -> list[QueuedRef]:
        """Посты источника в очереди, от самого слабого к самому сильному."""
        raise NotImplementedError

    async def priority_of(self, news_id: int, priority: Priority) -> float | None:
        """Приоритет новости."""
        raise NotImplementedError

    async def queue_counts_by_source(self, statuses: Sequence[NewsStatus]) -> dict[int, int]:
        """Размер очереди по источникам."""
        raise NotImplementedError


class LimitSourceRepo(Protocol):
    """Нужная лимиту часть SourceRepository."""

    async def lock(self, source_id: int) -> bool:
        """Блокирует источник до конца транзакции."""
        raise NotImplementedError


@dataclass(frozen=True)
class QueueLimitRepos:
    """Репозитории одной открытой транзакции."""

    news: LimitNewsRepo
    sources: LimitSourceRepo


class AdmitOutcome(StrEnum):
    """Итог допуска поста в очередь."""

    ADMITTED = "admitted"
    ADMITTED_DISPLACED = "admitted_displaced"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AdmitResult:
    """Результат допуска.

    Attributes:
        outcome: Итог.
        status: Статус новости после допуска.
        displaced_id: Вытесненный пост источника, если он был.
    """

    outcome: AdmitOutcome
    status: NewsStatus
    displaced_id: int | None = None


async def admit_to_queue(
    news_id: int,
    source_id: int,
    expected: NewsStatus,
    repos: QueueLimitRepos,
    max_per_source: int,
    priority: Priority,
    *,
    forced: bool = False,
) -> AdmitResult:
    """Ставит пост в очередь, вытесняя слабейший пост источника при переполнении.

    Вызывается внутри уже открытой транзакции. Источник блокируется, поэтому две
    новости одного источника не пройдут лимит одновременно. Решение администратора
    (forced) попадает в очередь всегда.

    Args:
        news_id: Новость.
        source_id: Её источник.
        expected: Текущий статус новости.
        repos: Репозитории открытой транзакции.
        max_per_source: Сколько постов источника держать в очереди; 0 — без лимита.
        priority: Параметры приоритета: старение оценки и бонус за источники.
        forced: Решение администратора — поставить в очередь несмотря на лимит.

    Returns:
        Итог допуска и статус новости.

    Raises:
        ValueError: Не-forced вызов не из processed или у новости нет пересказа.
    """
    if not forced and expected is not NewsStatus.PROCESSED:
        raise ValueError(
            f"без forced допуск возможен только из processed, получено {expected.value}"
        )
    if max_per_source == 0:
        await repos.news.set_status(news_id, expected, NewsStatus.APPROVED)
        return AdmitResult(AdmitOutcome.ADMITTED, NewsStatus.APPROVED)

    await repos.sources.lock(source_id)
    queued = await repos.news.queued_by_source(source_id, QUEUE_STATUSES, priority)
    if len(queued) < max_per_source:
        await repos.news.set_status(news_id, expected, NewsStatus.APPROVED)
        return _done(news_id, source_id, AdmitResult(AdmitOutcome.ADMITTED, NewsStatus.APPROVED))

    mine = await repos.news.priority_of(news_id, priority)
    if mine is None:
        raise ValueError(f"у новости {news_id} нет пересказа: приоритет неизвестен")
    weakest = queued[0]

    if forced or mine > weakest.priority:
        await repos.news.set_status(
            weakest.news_id, weakest.status, NewsStatus.FILTERED, REASON_QUEUE_LIMIT
        )
        await repos.news.set_status(news_id, expected, NewsStatus.APPROVED)
        result = AdmitResult(
            AdmitOutcome.ADMITTED_DISPLACED, NewsStatus.APPROVED, displaced_id=weakest.news_id
        )
    else:
        await repos.news.set_status(news_id, expected, NewsStatus.FILTERED, REASON_QUEUE_LIMIT)
        result = AdmitResult(AdmitOutcome.REJECTED, NewsStatus.FILTERED)
    return _done(news_id, source_id, result, mine=mine, weakest=weakest.priority)


def _done(
    news_id: int,
    source_id: int,
    result: AdmitResult,
    mine: float | None = None,
    weakest: float | None = None,
) -> AdmitResult:
    log.info(
        "queue_limit.admit",
        news_id=news_id,
        source_id=source_id,
        outcome=result.outcome.value,
        displaced_id=result.displaced_id,
        priority=mine,
        weakest_priority=weakest,
    )
    return result


async def trim_queue(repos: QueueLimitRepos, max_per_source: int, priority: Priority) -> int:
    """Приводит очередь к лимиту: вытесняет лишние посты каждого источника.

    Нужен, когда лимит уменьшили или очередь успела переполниться.

    Args:
        repos: Репозитории открытой транзакции.
        max_per_source: Сколько постов источника оставить; 0 — ничего не делать.
        priority: Параметры приоритета: старение оценки и бонус за источники.

    Returns:
        Сколько постов вытеснено.
    """
    if max_per_source == 0:
        return 0
    counts = await repos.news.queue_counts_by_source(QUEUE_STATUSES)
    displaced = 0
    for source_id, count in sorted(counts.items()):
        if count <= max_per_source:
            continue
        await repos.sources.lock(source_id)
        queued = await repos.news.queued_by_source(source_id, QUEUE_STATUSES, priority)
        for item in queued[: max(len(queued) - max_per_source, 0)]:
            await repos.news.set_status(
                item.news_id, item.status, NewsStatus.FILTERED, REASON_QUEUE_LIMIT
            )
            displaced += 1
    if displaced:
        log.info("queue_limit.trimmed", displaced=displaced, max_per_source=max_per_source)
    return displaced


@dataclass(frozen=True)
class QueueAdmission:
    """Общий вход в очередь: одна настройка лимита на все пути постановки.

    Attributes:
        news_repo, source_repo: Фабрики репозиториев от сессии.
        max_per_source: Постов источника в очереди (publisher.max_queued_per_source).
        priority: Параметры приоритета очереди (секция publisher).
        runtime: Изменяемые на лету параметры; None — значения из yaml.
    """

    news_repo: Callable[[Any], LimitNewsRepo]
    source_repo: Callable[[Any], LimitSourceRepo]
    max_per_source: int
    priority: Priority
    runtime: RuntimeSource | None = None

    async def admit(
        self,
        session: Any,
        news_id: int,
        source_id: int,
        expected: NewsStatus,
        *,
        forced: bool = False,
    ) -> AdmitResult:
        """Ставит пост в очередь в уже открытой транзакции."""
        repos = QueueLimitRepos(news=self.news_repo(session), sources=self.source_repo(session))
        max_per_source, priority = await self.limits()
        return await admit_to_queue(
            news_id,
            source_id,
            expected,
            repos,
            max_per_source,
            priority,
            forced=forced,
        )

    async def limits(self) -> tuple[int, Priority]:
        """Лимит источника и параметры приоритета на этот момент.

        Returns:
            Значения из снимка изменяемых параметров или из yaml.
        """
        if self.runtime is None:
            return self.max_per_source, self.priority
        publisher = (await self.runtime.snapshot()).publisher
        return publisher.max_queued_per_source, Priority.from_config(publisher)
