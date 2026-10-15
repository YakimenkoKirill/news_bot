from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
import structlog

from app.db.models import ProcessedPost
from app.db.repositories.news import StaleStatusError
from app.domain.enums import NewsStatus
from app.domain.status import ensure_transition
from app.pipeline.retry import RetryDeps, RetryOutcome, RetryResult, retry_failed

S = NewsStatus
NEWS_ID = 7


@dataclass
class News:
    id: int = NEWS_ID
    source_id: int = 1
    status: NewsStatus = S.FAILED


@dataclass
class Db:
    news: dict[int, News] = field(default_factory=dict)
    posts: dict[int, ProcessedPost] = field(default_factory=dict)
    stale: bool = False
    stolen_status: NewsStatus = S.NORMALIZED
    transitions: list[tuple[int, NewsStatus, NewsStatus]] = field(default_factory=list)


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, news_id: int) -> News | None:
        return self.db.news.get(news_id)

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        ensure_transition(expected, new)
        item = self.db.news[news_id]
        if self.db.stale:
            item.status = self.db.stolen_status
            raise StaleStatusError(news_id, expected)
        assert item.status == expected
        item.status = new
        self.db.transitions.append((news_id, expected, new))


class PostRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        return self.db.posts.get(news_id)


def _post(news_id: int = NEWS_ID) -> ProcessedPost:
    return ProcessedPost(
        news_item_id=news_id,
        ai_title="Заголовок",
        ai_summary="Пересказ",
        hashtags=[],
        relevance_score=0.8,
        prompt_version="v3",
        prompt_tokens=1,
        completion_tokens=1,
        cost=Decimal("0.001"),
    )


def _deps(db: Db) -> RetryDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    return RetryDeps(session_scope=scope, news_repo=NewsRepo, post_repo=PostRepo)


def _db(status: NewsStatus = S.FAILED, with_post: bool = False, **kwargs: object) -> Db:
    db = Db(news={NEWS_ID: News(status=status)}, **kwargs)  # type: ignore[arg-type]
    if with_post:
        db.posts[NEWS_ID] = _post()
    return db


async def test_failed_without_post_goes_to_ai() -> None:
    db = _db()
    result = await retry_failed(NEWS_ID, _deps(db))
    assert result == RetryResult(RetryOutcome.TO_AI, S.NORMALIZED)
    assert db.news[NEWS_ID].status is S.NORMALIZED
    assert db.transitions == [(NEWS_ID, S.FAILED, S.NORMALIZED)]


async def test_failed_with_post_goes_to_queue() -> None:
    db = _db(with_post=True)
    result = await retry_failed(NEWS_ID, _deps(db))
    assert result == RetryResult(RetryOutcome.TO_QUEUE, S.APPROVED)
    assert db.news[NEWS_ID].status is S.APPROVED
    assert db.transitions == [(NEWS_ID, S.FAILED, S.APPROVED)]


@pytest.mark.parametrize("status", [S.PUBLISHED, S.APPROVED, S.NORMALIZED, S.REJECTED])
async def test_other_status_not_failed(status: NewsStatus) -> None:
    db = _db(status=status)
    result = await retry_failed(NEWS_ID, _deps(db))
    assert result == RetryResult(RetryOutcome.NOT_FAILED, status)
    assert db.transitions == []


async def test_second_retry_is_not_failed() -> None:
    db = _db()
    assert (await retry_failed(NEWS_ID, _deps(db))).outcome is RetryOutcome.TO_AI
    second = await retry_failed(NEWS_ID, _deps(db))
    assert second == RetryResult(RetryOutcome.NOT_FAILED, S.NORMALIZED)
    assert len(db.transitions) == 1


async def test_missing_news_not_found() -> None:
    result = await retry_failed(999, _deps(_db()))
    assert result == RetryResult(RetryOutcome.NOT_FOUND, None)


async def test_parallel_retry_reports_current_status() -> None:
    db = _db(stale=True, stolen_status=S.APPROVED)
    result = await retry_failed(NEWS_ID, _deps(db))
    assert result == RetryResult(RetryOutcome.NOT_FAILED, S.APPROVED)
    assert db.transitions == []


async def test_outcome_logged() -> None:
    with structlog.testing.capture_logs() as logs:
        await retry_failed(NEWS_ID, _deps(_db(with_post=True)))
    [entry] = [e for e in logs if e["event"] == "retry.failed_news"]
    assert (entry["news_id"], entry["outcome"]) == (NEWS_ID, "to_queue")


def test_real_repositories_fit_protocols() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository

    deps = RetryDeps(
        session_scope=MagicMock(), news_repo=NewsRepository, post_repo=ProcessedPostRepository
    )
    assert deps.news_repo is NewsRepository


class Admission:
    """Подменяет постановку в очередь, запоминая аргументы."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, NewsStatus, bool]] = []

    async def admit(
        self,
        session: object,
        news_id: int,
        source_id: int,
        expected: NewsStatus,
        *,
        forced: bool = False,
    ) -> object:
        self.calls.append((news_id, source_id, expected, forced))
        self.db_status(news_id)
        return object()

    db: Db | None = None

    def db_status(self, news_id: int) -> None:
        if self.db is not None:
            self.db.news[news_id].status = S.APPROVED


def _deps_with_admission(db: Db, admission: Admission) -> RetryDeps:
    base = _deps(db)
    admission.db = db
    return RetryDeps(
        session_scope=base.session_scope,
        news_repo=base.news_repo,
        post_repo=base.post_repo,
        admission=admission,  # type: ignore[arg-type]
    )


async def test_retry_to_queue_goes_through_admission() -> None:
    db = _db(with_post=True)
    admission = Admission()
    result = await retry_failed(NEWS_ID, _deps_with_admission(db, admission))

    assert result == RetryResult(RetryOutcome.TO_QUEUE, S.APPROVED)
    assert admission.calls == [(NEWS_ID, 1, S.FAILED, True)], "решение админа — forced"
    assert db.transitions == [], "статус меняет admit, а не прямой set_status"


async def test_retry_to_ai_does_not_use_admission() -> None:
    db = _db()
    admission = Admission()
    result = await retry_failed(NEWS_ID, _deps_with_admission(db, admission))

    assert result.outcome is RetryOutcome.TO_AI
    assert admission.calls == []
    assert db.news[NEWS_ID].status is S.NORMALIZED
