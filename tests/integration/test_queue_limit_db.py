import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import NewsItem, ProcessedPost, Source
from app.db.repositories.news import NewsRepository, Priority
from app.db.repositories.sources import SourceRepository
from app.domain.enums import NewsStatus, SourceType
from app.pipeline.queue_limit import (
    QUEUE_STATUSES,
    REASON_QUEUE_LIMIT,
    AdmitOutcome,
    QueueLimitRepos,
    admit_to_queue,
    trim_queue,
)

pytestmark = pytest.mark.integration

S = NewsStatus
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
DECAY = 0.02
PRIORITY = Priority(DECAY)


class Fixture:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.counter = 0

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    def repos(self, session: AsyncSession) -> QueueLimitRepos:
        return QueueLimitRepos(news=NewsRepository(session), sources=SourceRepository(session))

    async def source(self, name: str) -> int:
        async with self.scope() as session:
            source = Source(
                type=SourceType.RSS, url=f"https://{name}.example/rss", name=name, interval_min=10
            )
            session.add(source)
            await session.flush()
            return source.id

    async def news(self, source_id: int, score: float, status: NewsStatus = S.APPROVED) -> int:
        async with self.scope() as session:
            self.counter += 1
            item = NewsItem(
                source_id=source_id,
                external_id=str(self.counter),
                url=f"https://e.example/{self.counter}",
                title=f"Новость {self.counter}",
                raw_text="текст",
                status=status,
                published_at=NOW,
            )
            session.add(item)
            await session.flush()
            session.add(
                ProcessedPost(
                    news_item_id=item.id,
                    ai_title=f"Заголовок {self.counter}",
                    ai_summary="Пересказ",
                    hashtags=[],
                    relevance_score=score,
                    prompt_version="v3",
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost=Decimal("0.000001"),
                )
            )
            return item.id

    async def statuses(self) -> dict[int, NewsStatus]:
        async with self.scope() as session:
            repo = NewsRepository(session)
            result: dict[int, NewsStatus] = {}
            for news_id in range(1, self.counter + 1):
                item = await repo.get(news_id)
                if item is not None:
                    result[news_id] = item.status
            return result

    async def queue(self, source_id: int) -> list[int]:
        async with self.scope() as session:
            queued = await NewsRepository(session).queued_by_source(
                source_id, QUEUE_STATUSES, PRIORITY
            )
        return [item.news_id for item in queued]


async def test_parallel_admits_respect_limit_of_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Два допуска одного источника при лимите 1: в очереди остаётся сильнейший."""
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    weak = await fixture.news(source_id, 0.3, status=S.PROCESSED)
    strong = await fixture.news(source_id, 0.9, status=S.PROCESSED)

    async def admit(news_id: int) -> AdmitOutcome:
        async with fixture.scope() as session:
            result = await admit_to_queue(
                news_id, source_id, S.PROCESSED, fixture.repos(session), 1, PRIORITY
            )
            return result.outcome

    await asyncio.gather(admit(weak), admit(strong))

    assert await fixture.queue(source_id) == [strong], "в очереди один — сильнейший"
    statuses = await fixture.statuses()
    assert statuses[strong] is S.APPROVED
    assert statuses[weak] is S.FILTERED


async def test_parallel_admits_keep_limit_of_two(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    candidates = [await fixture.news(source_id, score, S.PROCESSED) for score in (0.2, 0.5, 0.9)]

    async def admit(news_id: int) -> None:
        async with fixture.scope() as session:
            await admit_to_queue(
                news_id, source_id, S.PROCESSED, fixture.repos(session), 2, PRIORITY
            )

    await asyncio.gather(*(admit(news_id) for news_id in candidates))

    queue = await fixture.queue(source_id)
    assert len(queue) == 2
    assert set(queue) == {candidates[1], candidates[2]}, "остались сильнейшие"


async def test_admit_displaces_weakest_on_real_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    weak = await fixture.news(source_id, 0.2)
    kept = await fixture.news(source_id, 0.8)
    fresh = await fixture.news(source_id, 0.9, status=S.PROCESSED)

    async with fixture.scope() as session:
        result = await admit_to_queue(
            fresh, source_id, S.PROCESSED, fixture.repos(session), 2, PRIORITY
        )
    assert result.outcome is AdmitOutcome.ADMITTED_DISPLACED
    assert result.displaced_id == weak
    assert sorted(await fixture.queue(source_id)) == sorted([kept, fresh])

    async with fixture.scope() as session:
        displaced = await NewsRepository(session).get(weak)
    assert displaced is not None
    assert displaced.reject_reason == REASON_QUEUE_LIMIT


async def test_trim_queue_on_real_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    first = await fixture.source("A")
    second = await fixture.source("B")
    for score in (0.1, 0.2, 0.3, 0.9, 0.8):
        await fixture.news(first, score)
    kept_second = [await fixture.news(second, score) for score in (0.4, 0.5)]

    async with fixture.scope() as session:
        displaced = await trim_queue(fixture.repos(session), 2, PRIORITY)

    assert displaced == 3
    assert len(await fixture.queue(first)) == 2
    assert sorted(await fixture.queue(second)) == sorted(kept_second)


async def test_trim_queue_without_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    for score in (0.1, 0.2, 0.3):
        await fixture.news(source_id, score)

    async with fixture.scope() as session:
        assert await trim_queue(fixture.repos(session), 0, PRIORITY) == 0
    assert len(await fixture.queue(source_id)) == 3
