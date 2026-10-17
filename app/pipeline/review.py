"""Отмена постов на модерации: один пост и все сразу.

Одной и той же функцией пользуются кнопка «Отклонить» под черновиком и кнопки
отмены в /queue, поэтому пост всегда отменяется одинаково: статус rejected,
причина rejected_by_moderator и снятая отсрочка «Позже».
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import structlog

from app.db.repositories.news import StaleStatusError
from app.domain.enums import NewsStatus

log = structlog.get_logger(__name__)

REJECT_REASON = "rejected_by_moderator"


class ReviewNews(Protocol):
    """Новость на модерации (NewsItem подходит структурно)."""

    id: int
    status: NewsStatus


class ReviewNewsRepo(Protocol):
    """Нужная отмене часть NewsRepository."""

    async def get(self, news_id: int) -> ReviewNews | None:
        """Новость по id."""
        raise NotImplementedError

    async def set_status(
        self,
        news_id: int,
        expected: NewsStatus,
        new: NewsStatus,
        reason: str | None = None,
    ) -> None:
        """Смена статуса через машину состояний."""
        raise NotImplementedError

    async def claim_batch(
        self, status: NewsStatus | Sequence[NewsStatus], limit: int
    ) -> Sequence[ReviewNews]:
        """Захватывает пачку новостей (FOR UPDATE SKIP LOCKED)."""
        raise NotImplementedError


class ReviewPost(Protocol):
    """Пересказ новости (ProcessedPost подходит структурно)."""

    id: int


class ReviewPostRepo(Protocol):
    """Нужная отмене часть ProcessedPostRepository."""

    async def get_by_news(self, news_id: int) -> ReviewPost | None:
        """Пересказ новости."""
        raise NotImplementedError

    async def set_snooze(self, post_id: int, until: Any) -> bool:
        """Отсрочка показа на модерации; None — снять."""
        raise NotImplementedError


@dataclass(frozen=True)
class ReviewDeps:
    """Зависимости отмены постов на модерации.

    Attributes:
        session_scope: Открывает транзакцию БД.
        news_repo: Репозиторий новостей для сессии.
        post_repo: Репозиторий пересказов для сессии.
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    news_repo: Callable[[Any], ReviewNewsRepo]
    post_repo: Callable[[Any], ReviewPostRepo]


class CancelOutcome(StrEnum):
    """Итог отмены одного поста."""

    CANCELLED = "cancelled"
    ALREADY_DONE = "already_done"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class CancelResult:
    """Результат отмены.

    Attributes:
        outcome: Итог.
        status: Текущий статус новости (None — новости нет).
    """

    outcome: CancelOutcome
    status: NewsStatus | None = None


async def cancel_review(news_id: int, by: int, deps: ReviewDeps) -> CancelResult:
    """Отменяет один пост на модерации.

    Статус меняется и отсрочка «Позже» снимается в одной транзакции, поэтому
    отменённый пост не всплывёт в resend_due. Если пост в это время решил другой
    администратор, возвращается already_done с перечитанным статусом.

    Args:
        news_id: Новость.
        by: Telegram ID администратора — для лога.
        deps: Зависимости.

    Returns:
        Результат: cancelled, already_done или not_found.
    """
    async with deps.session_scope() as session:
        news = await deps.news_repo(session).get(news_id)
        if news is None:
            return CancelResult(CancelOutcome.NOT_FOUND)
        if news.status is not NewsStatus.PENDING_REVIEW:
            return CancelResult(CancelOutcome.ALREADY_DONE, news.status)
        try:
            await _reject(news_id, session, deps)
        except StaleStatusError:
            return CancelResult(CancelOutcome.ALREADY_DONE, await _status(news_id, deps))
    log.info("review.cancelled", news_id=news_id, by=by)
    return CancelResult(CancelOutcome.CANCELLED, NewsStatus.REJECTED)


async def cancel_all_reviews(by: int, deps: ReviewDeps, batch_size: int) -> int:
    """Отменяет все посты на модерации пачками.

    Пачка захватывается с SKIP LOCKED: пост, который в эту секунду одобряет
    другой администратор, пропускается и остаётся одобренным.

    Args:
        by: Telegram ID администратора — для лога.
        deps: Зависимости.
        batch_size: Размер пачки (pipeline.batch_size).

    Returns:
        Сколько постов отменено.

    Raises:
        ValueError: batch_size <= 0.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size должен быть положительным, получено {batch_size}")
    cancelled = 0
    while True:
        async with deps.session_scope() as session:
            batch = list(
                await deps.news_repo(session).claim_batch(NewsStatus.PENDING_REVIEW, batch_size)
            )
            for news in batch:
                await _reject(news.id, session, deps)
        cancelled += len(batch)
        if len(batch) < batch_size:
            break
    log.info("review.cancelled_all", count=cancelled, by=by)
    return cancelled


async def _reject(news_id: int, session: Any, deps: ReviewDeps) -> None:
    """Переводит пост в rejected и снимает отсрочку «Позже»."""
    await deps.news_repo(session).set_status(
        news_id, NewsStatus.PENDING_REVIEW, NewsStatus.REJECTED, REJECT_REASON
    )
    post = await deps.post_repo(session).get_by_news(news_id)
    if post is not None:
        await deps.post_repo(session).set_snooze(post.id, None)


async def _status(news_id: int, deps: ReviewDeps) -> NewsStatus | None:
    async with deps.session_scope() as session:
        news = await deps.news_repo(session).get(news_id)
    return news.status if news is not None else None
