import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import NewsItem, ProcessedPost, Source
from app.db.repositories.news import NewsRepository, Priority
from app.db.repositories.sources import SourceRepository
from app.domain.enums import NewsStatus, SourceType

pytestmark = pytest.mark.integration

S = NewsStatus
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
QUEUE = [S.APPROVED, S.SCHEDULED]
DECAY = 0.02
PRIORITY = Priority(DECAY)
LOCK_TIMEOUT = 0.3


class Fixture:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.counter = 0

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    async def source(self, name: str) -> int:
        async with self.scope() as session:
            source = Source(
                type=SourceType.RSS, url=f"https://{name}.example/rss", name=name, interval_min=10
            )
            session.add(source)
            await session.flush()
            return source.id

    async def news(
        self,
        source_id: int,
        score: float | None = 0.5,
        *,
        status: NewsStatus = S.APPROVED,
        published_at: datetime = NOW,
    ) -> int:
        async with self.scope() as session:
            self.counter += 1
            item = NewsItem(
                source_id=source_id,
                external_id=str(self.counter),
                url=f"https://e.example/{self.counter}",
                title=f"Новость {self.counter}",
                raw_text="текст",
                status=status,
                published_at=published_at,
            )
            session.add(item)
            await session.flush()
            if score is not None:
                session.add(
                    ProcessedPost(
                        news_item_id=item.id,
                        ai_title="Заголовок",
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


async def test_lock_existing_and_missing_source(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")

    repo = SourceRepository(session)
    assert await repo.lock(source_id) is True
    assert await repo.lock(10_000) is False


async def test_second_lock_waits_for_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    held = asyncio.Event()
    released = asyncio.Event()

    async def holder() -> None:
        async with session_scope(session_factory) as session:
            assert await SourceRepository(session).lock(source_id) is True
            held.set()
            await asyncio.sleep(0.05)
            released.set()

    async def waiter() -> bool:
        await held.wait()
        async with session_scope(session_factory) as session:
            locked = await SourceRepository(session).lock(source_id)
        assert released.is_set(), "вторая сессия не должна пройти до коммита первой"
        return locked

    _, locked = await asyncio.wait_for(
        asyncio.gather(holder(), waiter()), timeout=LOCK_TIMEOUT * 10
    )
    assert locked is True


async def test_lock_of_other_source_does_not_wait(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    first = await fixture.source("A")
    second = await fixture.source("B")

    async with session_scope(session_factory) as holder:
        assert await SourceRepository(holder).lock(first) is True

        async def other() -> bool:
            async with session_scope(session_factory) as session:
                return await SourceRepository(session).lock(second)

        assert await asyncio.wait_for(other(), timeout=LOCK_TIMEOUT) is True


async def test_queued_by_source_weakest_first(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    strong = await fixture.news(source_id, 0.9)
    weak = await fixture.news(source_id, 0.2)
    middle = await fixture.news(source_id, 0.5)

    repo = NewsRepository(session)
    queued = await repo.queued_by_source(source_id, QUEUE, PRIORITY)
    assert [item.news_id for item in queued] == [weak, middle, strong]

    prepared = await repo.list_prepared(QUEUE, PRIORITY, limit=10, offset=0)
    assert [news.id for news, _ in prepared] == [strong, middle, weak], "порядок обратный"


async def test_queued_by_source_matches_priority_of(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    fresh = await fixture.news(source_id, 0.6, published_at=NOW)
    old = await fixture.news(source_id, 0.8, published_at=NOW - timedelta(hours=12))

    repo = NewsRepository(session)
    rows = await repo.queued_by_source(source_id, QUEUE, PRIORITY)
    queued = {item.news_id: item.priority for item in rows}
    assert queued[fresh] == await repo.priority_of(fresh, PRIORITY)
    assert queued[old] == await repo.priority_of(old, PRIORITY)
    assert queued[fresh] > queued[old], "свежая новость сильнее с поправкой на возраст"


async def test_equal_priority_bigger_id_is_weaker(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    first = await fixture.news(source_id, 0.5)
    second = await fixture.news(source_id, 0.5)

    queued = await NewsRepository(session).queued_by_source(source_id, QUEUE, PRIORITY)
    assert [item.news_id for item in queued] == [second, first]


async def test_queued_by_source_filters_statuses_and_sources(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    other_id = await fixture.source("B")
    approved = await fixture.news(source_id, 0.5, status=S.APPROVED)
    scheduled = await fixture.news(source_id, 0.4, status=S.SCHEDULED)
    await fixture.news(source_id, 0.9, status=S.PENDING_REVIEW)
    await fixture.news(source_id, None, status=S.APPROVED)
    await fixture.news(other_id, 0.9, status=S.APPROVED)

    queued = await NewsRepository(session).queued_by_source(source_id, QUEUE, PRIORITY)
    assert {item.news_id for item in queued} == {approved, scheduled}
    assert {item.status for item in queued} == {S.APPROVED, S.SCHEDULED}


async def test_queued_by_source_empty(session: AsyncSession) -> None:
    assert await NewsRepository(session).queued_by_source(999, QUEUE, PRIORITY) == []


async def test_priority_of_without_post_or_news(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    without_post = await fixture.news(source_id, None)

    repo = NewsRepository(session)
    assert await repo.priority_of(without_post, PRIORITY) is None
    assert await repo.priority_of(10_000, PRIORITY) is None


async def test_queue_counts_by_source(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    first = await fixture.source("A")
    second = await fixture.source("B")
    await fixture.news(first, 0.5)
    await fixture.news(first, 0.6, status=S.SCHEDULED)
    await fixture.news(first, None)
    await fixture.news(second, 0.5)
    await fixture.news(second, 0.5, status=S.PENDING_REVIEW)

    counts = await NewsRepository(session).queue_counts_by_source(QUEUE)
    assert counts == {first: 2, second: 1}


@pytest.mark.parametrize("decay", [-0.1, -1.0])
async def test_negative_decay_rejected(session: AsyncSession, decay: float) -> None:
    repo = NewsRepository(session)
    with pytest.raises(ValueError):
        await repo.queued_by_source(1, QUEUE, Priority(decay))
    with pytest.raises(ValueError):
        await repo.priority_of(1, Priority(decay))


async def test_empty_statuses_rejected(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    with pytest.raises(ValueError):
        await repo.queued_by_source(1, [], PRIORITY)
    with pytest.raises(ValueError):
        await repo.queue_counts_by_source([])
