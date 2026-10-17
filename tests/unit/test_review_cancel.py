from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
import structlog.testing

from app.db.repositories.news import StaleStatusError
from app.domain.enums import NewsStatus
from app.domain.status import ensure_transition
from app.pipeline.review import (
    REJECT_REASON,
    CancelOutcome,
    CancelResult,
    ReviewDeps,
    cancel_all_reviews,
    cancel_review,
)

ADMIN = 111
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
BATCH = 20
S = NewsStatus


@dataclass
class News:
    id: int
    status: NewsStatus = S.PENDING_REVIEW


@dataclass
class Post:
    id: int
    news_item_id: int
    snoozed_until: datetime | None = NOW


@dataclass
class Db:
    news: dict[int, News] = field(default_factory=dict)
    posts: dict[int, Post] = field(default_factory=dict)
    reasons: dict[int, str] = field(default_factory=dict)
    busy: set[int] = field(default_factory=set)
    stale: set[int] = field(default_factory=set)
    claims: list[int] = field(default_factory=list)

    def add(self, news_id: int, status: NewsStatus = S.PENDING_REVIEW, post: bool = True) -> int:
        self.news[news_id] = News(news_id, status)
        if post:
            self.posts[news_id] = Post(id=news_id * 10, news_item_id=news_id)
        return news_id


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, news_id: int) -> News | None:
        return self.db.news.get(news_id)

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        item = self.db.news[news_id]
        if news_id in self.db.stale or item.status is not expected:
            raise StaleStatusError(news_id, expected)
        ensure_transition(expected, new)
        item.status = new
        if reason:
            self.db.reasons[news_id] = reason

    async def claim_batch(
        self, status: NewsStatus | Sequence[NewsStatus], limit: int
    ) -> list[News]:
        """Пачка без занятых строк — как SELECT ... FOR UPDATE SKIP LOCKED."""
        statuses = [status] if isinstance(status, NewsStatus) else list(status)
        batch = [
            item
            for item in sorted(self.db.news.values(), key=lambda n: n.id)
            if item.status in statuses and item.id not in self.db.busy
        ][:limit]
        self.db.claims.append(len(batch))
        return batch


class PostRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get_by_news(self, news_id: int) -> Post | None:
        return self.db.posts.get(news_id)

    async def set_snooze(self, post_id: int, until: datetime | None) -> bool:
        for post in self.db.posts.values():
            if post.id == post_id:
                post.snoozed_until = until
                return True
        return False


def _deps(db: Db) -> ReviewDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    return ReviewDeps(session_scope=scope, news_repo=NewsRepo, post_repo=PostRepo)


async def test_cancel_rejects_and_clears_snooze() -> None:
    db = Db()
    news_id = db.add(1)

    with structlog.testing.capture_logs() as logs:
        result = await cancel_review(news_id, ADMIN, _deps(db))

    assert result == CancelResult(CancelOutcome.CANCELLED, S.REJECTED)
    assert db.news[news_id].status is S.REJECTED
    assert db.reasons[news_id] == REJECT_REASON
    assert db.posts[news_id].snoozed_until is None, "отложенный пост не всплывёт в resend_due"
    [entry] = [e for e in logs if e["event"] == "review.cancelled"]
    assert (entry["news_id"], entry["by"]) == (news_id, ADMIN)


async def test_cancel_without_post_still_rejects() -> None:
    db = Db()
    news_id = db.add(1, post=False)
    assert (await cancel_review(news_id, ADMIN, _deps(db))).outcome is CancelOutcome.CANCELLED
    assert db.news[news_id].status is S.REJECTED


@pytest.mark.parametrize("status", [S.APPROVED, S.PUBLISHED, S.REJECTED, S.SCHEDULED])
async def test_cancel_of_decided_post_is_already_done(status: NewsStatus) -> None:
    db = Db()
    news_id = db.add(1, status=status)

    result = await cancel_review(news_id, ADMIN, _deps(db))

    assert result == CancelResult(CancelOutcome.ALREADY_DONE, status)
    assert db.news[news_id].status is status
    assert db.posts[news_id].snoozed_until == NOW


async def test_cancel_of_unknown_news() -> None:
    assert await cancel_review(7, ADMIN, _deps(Db())) == CancelResult(CancelOutcome.NOT_FOUND)


async def test_race_with_other_admin_gives_already_done() -> None:
    """Второй админ решил пост между чтением и записью: статус не перетирается."""
    db = Db()
    news_id = db.add(1)
    db.stale.add(news_id)

    result = await cancel_review(news_id, ADMIN, _deps(db))

    assert result.outcome is CancelOutcome.ALREADY_DONE
    assert result.status is S.PENDING_REVIEW
    assert db.news[news_id].status is S.PENDING_REVIEW


async def test_double_cancel_is_safe() -> None:
    db = Db()
    news_id = db.add(1)
    deps = _deps(db)

    first = await cancel_review(news_id, ADMIN, deps)
    second = await cancel_review(news_id, ADMIN, deps)

    assert first.outcome is CancelOutcome.CANCELLED
    assert second == CancelResult(CancelOutcome.ALREADY_DONE, S.REJECTED)


async def test_cancel_all_rejects_every_pending_post() -> None:
    db = Db()
    for news_id in (1, 2, 3):
        db.add(news_id)
    db.add(4, status=S.APPROVED)

    with structlog.testing.capture_logs() as logs:
        cancelled = await cancel_all_reviews(ADMIN, _deps(db), BATCH)

    assert cancelled == 3
    assert [db.news[i].status for i in (1, 2, 3)] == [S.REJECTED] * 3
    assert db.news[4].status is S.APPROVED
    assert all(db.posts[i].snoozed_until is None for i in (1, 2, 3))
    [entry] = [e for e in logs if e["event"] == "review.cancelled_all"]
    assert (entry["count"], entry["by"]) == (3, ADMIN)


async def test_cancel_all_on_empty_review_returns_zero() -> None:
    db = Db()
    db.add(1, status=S.PUBLISHED)
    assert await cancel_all_reviews(ADMIN, _deps(db), BATCH) == 0
    assert db.news[1].status is S.PUBLISHED


async def test_cancel_all_skips_locked_post() -> None:
    """Пост, который в эту секунду одобряет другой админ, пропускается."""
    db = Db()
    for news_id in (1, 2):
        db.add(news_id)
    db.busy.add(2)

    cancelled = await cancel_all_reviews(ADMIN, _deps(db), BATCH)

    assert cancelled == 1
    assert db.news[1].status is S.REJECTED
    assert db.news[2].status is S.PENDING_REVIEW


async def test_cancel_all_works_in_batches() -> None:
    db = Db()
    for news_id in range(1, 6):
        db.add(news_id)

    cancelled = await cancel_all_reviews(ADMIN, _deps(db), 2)

    assert cancelled == 5
    assert db.claims == [2, 2, 1]
    assert all(news.status is S.REJECTED for news in db.news.values())


async def test_cancel_all_full_last_batch_stops_on_empty() -> None:
    """Последняя пачка полная — нужен ещё один заход, чтобы убедиться, что постов нет."""
    db = Db()
    for news_id in (1, 2):
        db.add(news_id)

    assert await cancel_all_reviews(ADMIN, _deps(db), 2) == 2
    assert db.claims == [2, 0]


@pytest.mark.parametrize("batch_size", [0, -1])
async def test_non_positive_batch_rejected(batch_size: int) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        await cancel_all_reviews(ADMIN, _deps(Db()), batch_size)


def test_real_repositories_fit_protocols() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository

    deps = ReviewDeps(
        session_scope=MagicMock(),
        news_repo=NewsRepository,
        post_repo=ProcessedPostRepository,
    )
    assert isinstance(deps.news_repo(MagicMock()), NewsRepository)
    assert isinstance(deps.post_repo(MagicMock()), ProcessedPostRepository)


def test_reject_reason_shared_with_moderation() -> None:
    """Константа переехала в review, ссылки в модерации продолжают работать."""
    from app.bot.handlers.moderation import REJECT_REASON as from_moderation

    assert from_moderation == REJECT_REASON == "rejected_by_moderator"
