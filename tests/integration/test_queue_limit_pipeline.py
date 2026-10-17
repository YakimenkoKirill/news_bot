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
from app.pipeline.queue_limit import QUEUE_STATUSES, REASON_QUEUE_LIMIT, QueueAdmission

pytestmark = pytest.mark.integration

S = NewsStatus
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
DECAY = 0.02
PRIORITY = Priority(DECAY)
LIMIT = 2


class Pipe:
    """Постановка в очередь через QueueAdmission на реальной БД."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], limit: int = LIMIT) -> None:
        self.factory = factory
        self.counter = 0
        self.admission = QueueAdmission(
            news_repo=NewsRepository,
            source_repo=SourceRepository,
            max_per_source=limit,
            priority=PRIORITY,
        )

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

    async def processed(self, source_id: int, score: float) -> int:
        """Новость, которую модель уже обработала: статус processed с пересказом."""
        async with self.scope() as session:
            self.counter += 1
            item = NewsItem(
                source_id=source_id,
                external_id=str(self.counter),
                url=f"https://e.example/{self.counter}",
                title=f"Новость {self.counter}",
                raw_text="текст",
                status=S.PROCESSED,
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

    async def admit(self, news_id: int, source_id: int, **kwargs: object) -> object:
        async with self.scope() as session:
            return await self.admission.admit(
                session,
                news_id,
                source_id,
                S.PROCESSED,
                **kwargs,  # type: ignore[arg-type]
            )

    async def state(self, news_id: int) -> tuple[NewsStatus, str | None]:
        async with self.scope() as session:
            item = await NewsRepository(session).get(news_id)
            assert item is not None
            return item.status, item.reject_reason

    async def queue(self, source_id: int) -> set[int]:
        async with self.scope() as session:
            queued = await NewsRepository(session).queued_by_source(
                source_id, QUEUE_STATUSES, PRIORITY
            )
        return {item.news_id for item in queued}


async def test_five_approvals_keep_two_strongest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pipe = Pipe(session_factory)
    source_id = await pipe.source("A")
    scores = [0.3, 0.9, 0.5, 0.95, 0.4]
    news_ids = [await pipe.processed(source_id, score) for score in scores]

    for news_id in news_ids:
        await pipe.admit(news_id, source_id)

    by_score = dict(zip(news_ids, scores, strict=True))
    queue = await pipe.queue(source_id)
    assert len(queue) == LIMIT
    assert queue == {news_ids[1], news_ids[3]}, "остались 0.9 и 0.95"

    filtered = [news_id for news_id in news_ids if news_id not in queue]
    assert len(filtered) == 3
    for news_id in filtered:
        status, reason = await pipe.state(news_id)
        assert status is S.FILTERED
        assert reason == REASON_QUEUE_LIMIT, f"оценка {by_score[news_id]}"


async def test_limit_is_per_source(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pipe = Pipe(session_factory)
    first = await pipe.source("A")
    second = await pipe.source("B")
    for source_id in (first, second):
        for score in (0.4, 0.6, 0.8):
            await pipe.admit(await pipe.processed(source_id, score), source_id)

    assert len(await pipe.queue(first)) == LIMIT
    assert len(await pipe.queue(second)) == LIMIT


async def test_forced_admission_always_gets_in(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pipe = Pipe(session_factory)
    source_id = await pipe.source("A")
    for score in (0.8, 0.9):
        await pipe.admit(await pipe.processed(source_id, score), source_id)

    weak = await pipe.processed(source_id, 0.1)
    await pipe.admit(weak, source_id, forced=True)

    queue = await pipe.queue(source_id)
    assert weak in queue, "решение админа попадает в очередь"
    assert len(queue) == LIMIT


async def test_zero_limit_keeps_everything(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pipe = Pipe(session_factory, limit=0)
    source_id = await pipe.source("A")
    for score in (0.1, 0.2, 0.3, 0.4, 0.5):
        await pipe.admit(await pipe.processed(source_id, score), source_id)

    assert len(await pipe.queue(source_id)) == 5
