"""Повтор новостей, упавших с ошибкой."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import structlog

from app.db.models import ProcessedPost
from app.db.repositories.news import StaleStatusError
from app.domain.enums import NewsStatus

log = structlog.get_logger(__name__)


class RetryNews(Protocol):
    """Новость для повтора (NewsItem подходит структурно)."""

    id: int
    status: NewsStatus


class RetryNewsRepo(Protocol):
    """Нужная повтору часть NewsRepository."""

    async def get(self, news_id: int) -> RetryNews | None:
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


class RetryPostRepo(Protocol):
    """Нужная повтору часть ProcessedPostRepository."""

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        """Пересказ новости."""
        raise NotImplementedError


@dataclass(frozen=True)
class RetryDeps:
    """Зависимости повтора.

    Attributes:
        session_scope: Открывает транзакцию БД.
        news_repo, post_repo: Репозитории для сессии.
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    news_repo: Callable[[Any], RetryNewsRepo]
    post_repo: Callable[[Any], RetryPostRepo]


class RetryOutcome(StrEnum):
    """Итог повтора."""

    TO_AI = "to_ai"
    TO_QUEUE = "to_queue"
    NOT_FAILED = "not_failed"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class RetryResult:
    """Результат повтора.

    Attributes:
        outcome: Итог.
        status: Текущий статус новости (None, если её нет).
    """

    outcome: RetryOutcome
    status: NewsStatus | None = None


async def retry_failed(news_id: int, deps: RetryDeps) -> RetryResult:
    """Возвращает упавшую новость в нужную стадию.

    С пересказом — в approved (встанет в очередь публикации по приоритету),
    без пересказа — в normalized (модель обработает заново). reject_reason не
    очищается: при новой ошибке он перезапишется, а прошлая причина видна в истории.

    Args:
        news_id: Новость.
        deps: Зависимости.

    Returns:
        Итог: to_ai, to_queue, not_failed (статус уже другой или её перехватили) или not_found.
    """
    async with deps.session_scope() as session:
        news_repo = deps.news_repo(session)
        news = await news_repo.get(news_id)
        if news is None:
            result = RetryResult(RetryOutcome.NOT_FOUND)
        elif news.status is not NewsStatus.FAILED:
            result = RetryResult(RetryOutcome.NOT_FAILED, news.status)
        else:
            post = await deps.post_repo(session).get_by_news(news_id)
            target = NewsStatus.APPROVED if post is not None else NewsStatus.NORMALIZED
            outcome = RetryOutcome.TO_QUEUE if post is not None else RetryOutcome.TO_AI
            try:
                await news_repo.set_status(news_id, NewsStatus.FAILED, target)
            except StaleStatusError:
                current = await news_repo.get(news_id)
                result = RetryResult(
                    RetryOutcome.NOT_FAILED, current.status if current is not None else None
                )
            else:
                result = RetryResult(outcome, target)
    log.info("retry.failed_news", news_id=news_id, outcome=result.outcome.value)
    return result
