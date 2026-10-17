import asyncio
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import NewsItem, ProcessedPost, Source
from app.db.repositories.news import NewsRepository
from app.db.repositories.posts import ProcessedPostRepository
from app.domain.enums import NewsStatus, SourceType
from app.pipeline.review import (
    REJECT_REASON,
    CancelOutcome,
    ReviewDeps,
    cancel_all_reviews,
    cancel_review,
)

pytestmark = pytest.mark.integration

ADMIN = 111
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
BATCH = 20
S = NewsStatus


@dataclass(frozen=True)
class Raw:
    source_id: int
    external_id: str
    url: str
    title: str
    text: str
    image_url: str | None = None
    published_at: datetime | None = None
    raw_meta: Mapping[str, Any] = field(default_factory=dict)


class Fixture:
    """Посты на модерации в реальной БД."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.counter = 0
        self.deps = ReviewDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            post_repo=ProcessedPostRepository,
        )

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    async def post(
        self, status: NewsStatus = S.PENDING_REVIEW, snoozed_until: datetime | None = None
    ) -> int:
        async with self.scope() as session:
            source_id = await self._source(session)
            self.counter += 1
            item = await NewsRepository(session).add_if_new(
                Raw(
                    source_id=source_id,
                    external_id=str(self.counter),
                    url=f"https://r.example/{self.counter}",
                    title=f"Новость {self.counter}",
                    text="текст",
                )
            )
            assert item is not None
            await session.execute(
                update(NewsItem).where(NewsItem.id == item.id).values(status=status)
            )
            session.add(
                ProcessedPost(
                    news_item_id=item.id,
                    ai_title=f"Заголовок {self.counter}",
                    ai_summary="Пересказ новости.",
                    hashtags=[],
                    relevance_score=0.8,
                    prompt_version="v5",
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost=Decimal("0.000001"),
                    snoozed_until=snoozed_until,
                )
            )
            return item.id

    async def _source(self, session: AsyncSession) -> int:
        existing = await session.scalar(select(Source.id).limit(1))
        if existing is not None:
            return int(existing)
        source = Source(type=SourceType.RSS, url="https://r.example/rss", name="R", interval_min=10)
        session.add(source)
        await session.flush()
        return source.id

    async def status(self, news_id: int) -> NewsStatus:
        async with self.scope() as session:
            news = await NewsRepository(session).get(news_id)
            assert news is not None
            return news.status

    async def reason(self, news_id: int) -> str | None:
        async with self.scope() as session:
            news = await NewsRepository(session).get(news_id)
            assert news is not None
            return news.reject_reason

    async def due_reviews(self) -> list[int]:
        async with self.scope() as session:
            posts = await ProcessedPostRepository(session).list_due_reviews(NOW)
        return [post.news_item_id for post in posts]


async def test_cancel_rejects_in_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    news_id = await fixture.post()

    result = await cancel_review(news_id, ADMIN, fixture.deps)

    assert result.outcome is CancelOutcome.CANCELLED
    assert await fixture.status(news_id) is S.REJECTED
    assert await fixture.reason(news_id) == REJECT_REASON


async def test_cancelled_post_not_resent_to_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пост был отложен «Позже»: после отмены resend_due его не присылает."""
    fixture = Fixture(session_factory)
    news_id = await fixture.post(snoozed_until=NOW - timedelta(minutes=1))
    assert await fixture.due_reviews() == [news_id]

    await cancel_review(news_id, ADMIN, fixture.deps)

    assert await fixture.due_reviews() == []


async def test_cancel_of_approved_post_keeps_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    news_id = await fixture.post(status=S.APPROVED)

    result = await cancel_review(news_id, ADMIN, fixture.deps)

    assert result.outcome is CancelOutcome.ALREADY_DONE
    assert result.status is S.APPROVED
    assert await fixture.status(news_id) is S.APPROVED


async def test_cancel_all_rejects_every_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    pending = [await fixture.post() for _ in range(3)]
    approved = await fixture.post(status=S.APPROVED)

    assert await cancel_all_reviews(ADMIN, fixture.deps, BATCH) == 3

    for news_id in pending:
        assert await fixture.status(news_id) is S.REJECTED
    assert await fixture.status(approved) is S.APPROVED


async def test_cancel_all_skips_locked_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Строку держит другая сессия (другой админ решает пост) — он пропускается."""
    fixture = Fixture(session_factory)
    held = await fixture.post()
    free = await fixture.post()

    async with session_factory() as holder, holder.begin():
        locked = await NewsRepository(holder).claim_one(held, [S.PENDING_REVIEW])
        assert locked is not None

        cancelled = await cancel_all_reviews(ADMIN, fixture.deps, BATCH)

    assert cancelled == 1
    assert await fixture.status(free) is S.REJECTED
    assert await fixture.status(held) is S.PENDING_REVIEW


async def test_parallel_cancel_gives_one_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    news_id = await fixture.post()

    results = await asyncio.gather(
        cancel_review(news_id, ADMIN, fixture.deps),
        cancel_review(news_id, ADMIN + 1, fixture.deps),
    )

    outcomes = sorted(result.outcome.value for result in results)
    assert outcomes == ["already_done", "cancelled"]
    assert await fixture.status(news_id) is S.REJECTED


async def test_cancel_all_on_empty_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    await fixture.post(status=S.PUBLISHED)
    assert await cancel_all_reviews(ADMIN, fixture.deps, BATCH) == 0
