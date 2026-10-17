from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import NewsItem, ProcessedPost, Source
from app.db.repositories.news import NewsRepository, Priority
from app.db.repositories.posts import ProcessedPostRepository
from app.domain.enums import NewsStatus, SourceType
from app.pipeline.retry import RetryDeps, RetryOutcome, retry_failed

pytestmark = pytest.mark.integration

S = NewsStatus
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class Fixture:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self._source_id: int | None = None
        self._counter = 0

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    @property
    def retry_deps(self) -> RetryDeps:
        return RetryDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            post_repo=ProcessedPostRepository,
        )

    async def news(self, status: NewsStatus = S.FAILED, with_post: bool = False) -> int:
        async with self.scope() as session:
            if self._source_id is None:
                source = Source(
                    type=SourceType.RSS, url="https://f.example/rss", name="F", interval_min=10
                )
                session.add(source)
                await session.flush()
                self._source_id = source.id
            self._counter += 1
            item = NewsItem(
                source_id=self._source_id,
                external_id=str(self._counter),
                url=f"https://f.example/{self._counter}",
                title=f"Новость {self._counter}",
                raw_text="текст",
                clean_text="текст",
                status=status,
                reject_reason="bad_ai_response" if status is S.FAILED else None,
                published_at=NOW,
            )
            session.add(item)
            await session.flush()
            if with_post:
                session.add(
                    ProcessedPost(
                        news_item_id=item.id,
                        ai_title=f"Заголовок {self._counter}",
                        ai_summary="Пересказ новости.",
                        hashtags=[],
                        relevance_score=0.8,
                        prompt_version="v3",
                        prompt_tokens=1,
                        completion_tokens=1,
                        cost=Decimal("0.000001"),
                    )
                )
            return item.id

    async def status(self, news_id: int) -> NewsStatus:
        async with self.scope() as session:
            item = await NewsRepository(session).get(news_id)
            assert item is not None
            return item.status

    async def reason(self, news_id: int) -> str | None:
        async with self.scope() as session:
            item = await NewsRepository(session).get(news_id)
            assert item is not None
            return item.reject_reason


async def test_list_failed_newest_first_with_post_flag(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    no_post = await fixture.news()
    with_post = await fixture.news(with_post=True)
    await fixture.news(status=S.PUBLISHED, with_post=True)

    repo = NewsRepository(session)
    rows = await repo.list_failed(limit=10, offset=0)
    assert [(news.id, has_post) for news, has_post in rows] == [
        (with_post, True),
        (no_post, False),
    ]
    assert await repo.count_failed() == 2


async def test_list_failed_pagination(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    ids = [await fixture.news() for _ in range(4)]
    repo = NewsRepository(session)

    assert [n.id for n, _ in await repo.list_failed(limit=2, offset=0)] == ids[:-3:-1]
    assert [n.id for n, _ in await repo.list_failed(limit=2, offset=2)] == ids[1::-1]
    assert await repo.list_failed(limit=2, offset=10) == []


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (-1, 0), (10, -1)])
async def test_list_failed_invalid_arguments(
    session: AsyncSession, limit: int, offset: int
) -> None:
    with pytest.raises(ValueError):
        await NewsRepository(session).list_failed(limit=limit, offset=offset)


async def test_retry_without_post_returns_to_ai_stage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    news_id = await fixture.news()

    result = await retry_failed(news_id, fixture.retry_deps)
    assert result.outcome is RetryOutcome.TO_AI
    assert await fixture.status(news_id) is S.NORMALIZED
    assert await fixture.reason(news_id) == "bad_ai_response"

    async with fixture.scope() as session:
        claimed = await NewsRepository(session).claim_batch(S.NORMALIZED, 10)
        assert [item.id for item in claimed] == [news_id]


async def test_retry_with_post_returns_to_publish_queue(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    news_id = await fixture.news(with_post=True)

    result = await retry_failed(news_id, fixture.retry_deps)
    assert result.outcome is RetryOutcome.TO_QUEUE
    assert await fixture.status(news_id) is S.APPROVED

    async with fixture.scope() as session:
        claimed = await NewsRepository(session).claim_next([S.APPROVED, S.SCHEDULED], Priority(0.0))
        assert claimed is not None and claimed.id == news_id


async def test_second_retry_reports_not_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    news_id = await fixture.news(with_post=True)

    assert (await retry_failed(news_id, fixture.retry_deps)).outcome is RetryOutcome.TO_QUEUE
    second = await retry_failed(news_id, fixture.retry_deps)
    assert second.outcome is RetryOutcome.NOT_FAILED
    assert second.status is S.APPROVED


async def test_retry_missing_news(session_factory: async_sessionmaker[AsyncSession]) -> None:
    result = await retry_failed(10_000, Fixture(session_factory).retry_deps)
    assert result.outcome is RetryOutcome.NOT_FOUND
