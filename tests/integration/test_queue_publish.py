from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import PublisherConfig
from app.db.base import session_scope
from app.db.models import NewsItem, ProcessedPost, Publication, Source
from app.db.repositories.misc import SettingsRepository
from app.db.repositories.news import NewsRepository
from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
from app.db.repositories.sources import SourceRepository
from app.domain.enums import NewsStatus, PublicationStatus, SourceType
from app.publisher.publisher import Publisher
from app.publisher.stage_publish import (
    PUBLISHABLE_STATUSES,
    ManualOutcome,
    ManualPublishDeps,
    PublishDeps,
    publish_due,
    publish_now,
)
from tests.config_helpers import base_section
from tests.fakes import FakeTelegramBot, RecordingSink, image_never_ok, no_sleep

pytestmark = pytest.mark.integration

S = NewsStatus
CHANNEL = -1001234567890
ADMIN = 111
TZ = ZoneInfo("Europe/Moscow")
CFG = PublisherConfig(**{**base_section("publisher"), "quiet_hours": []})
INTERVAL = timedelta(minutes=CFG.min_interval_min)


class Env:
    """Публикация на реальной БД с фейковым ботом Telegram."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.telegram = FakeTelegramBot()
        self.publish = PublishDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            post_repo=ProcessedPostRepository,
            source_repo=SourceRepository,
            publication_repo=PublicationRepository,
            settings_repo=SettingsRepository,
            check_image=image_never_ok,
            publisher=Publisher(self.telegram, CHANNEL, CFG, sleep=no_sleep),
            sink=RecordingSink(),
            cfg=CFG,
            channel_id=CHANNEL,
            tz=TZ,
        )
        self.manual = ManualPublishDeps(publish=self.publish, news_repo=NewsRepository)
        self._source_id: int | None = None
        self._counter = 0

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    async def post(
        self, score: float, published_at: datetime, status: NewsStatus = S.APPROVED
    ) -> int:
        async with self.scope() as session:
            if self._source_id is None:
                source = Source(
                    type=SourceType.RSS, url="https://q.example/rss", name="Q", interval_min=10
                )
                session.add(source)
                await session.flush()
                self._source_id = source.id
            self._counter += 1
            news = NewsItem(
                source_id=self._source_id,
                external_id=str(self._counter),
                url=f"https://q.example/{self._counter}",
                title=f"Новость {self._counter}",
                raw_text="текст",
                status=status,
                published_at=published_at,
            )
            session.add(news)
            await session.flush()
            session.add(
                ProcessedPost(
                    news_item_id=news.id,
                    ai_title=f"Заголовок {self._counter}",
                    ai_summary="Пересказ новости своими словами.",
                    hashtags=["#тест"],
                    relevance_score=score,
                    prompt_version="v2",
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost=Decimal("0.000001"),
                )
            )
            return news.id

    async def published_minutes_ago(self, now: datetime, minutes: int) -> None:
        news_id = await self.post(0.1, now - timedelta(hours=1), status=S.PUBLISHED)
        async with self.scope() as session:
            post = await ProcessedPostRepository(session).get_by_news(news_id)
            assert post is not None
            await PublicationRepository(session).add(
                Publication(
                    processed_post_id=post.id,
                    channel_id=CHANNEL,
                    status=PublicationStatus.SENT,
                    message_id=1,
                    published_at=now - timedelta(minutes=minutes),
                )
            )

    async def status(self, news_id: int) -> NewsStatus:
        async with self.scope() as session:
            news = await NewsRepository(session).get(news_id)
            assert news is not None
            return news.status

    async def queue_ids(self) -> list[int]:
        async with self.scope() as session:
            rows = await NewsRepository(session).list_prepared(
                PUBLISHABLE_STATUSES, CFG.score_decay_per_h, limit=100, offset=0
            )
        return [news.id for news, _ in rows]


async def test_publish_due_follows_priority(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    env = Env(session_factory)
    now = pipeline_now
    old = await env.post(0.8, now - timedelta(hours=12))
    fresh = await env.post(0.6, now)
    stale_top = await env.post(0.9, now - timedelta(hours=30))
    expected = await env.queue_ids()
    assert expected == [fresh, old, stale_top]

    published = []
    for step in range(3):
        report = await publish_due(now + step * INTERVAL, env.publish)
        published.append(report.news_id)
    assert published == expected
    assert len(env.telegram.sent) == 3
    assert await env.queue_ids() == []


async def test_publish_now_outside_queue(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    env = Env(session_factory)
    now = pipeline_now
    top = await env.post(0.9, now)
    low = await env.post(0.5, now)
    await env.published_minutes_ago(now, 1)

    assert (await publish_due(now, env.publish)).skipped == "slot"

    result = await publish_now(low, now, env.manual, ADMIN)
    assert result.outcome is ManualOutcome.PUBLISHED
    assert await env.status(low) is S.PUBLISHED
    assert await env.status(top) is S.APPROVED
    async with env.scope() as session:
        assert await PublicationRepository(session).last_published_at(CHANNEL) == now
    sent_text = env.telegram.sent[-1]["text"]
    assert "Заголовок 2" in sent_text

    report = await publish_due(now, env.publish)
    assert report.skipped == "slot"
    assert report.next_slot == now + INTERVAL

    sent_before = len(env.telegram.sent)
    async with session_factory() as holder, holder.begin():
        [held] = await NewsRepository(holder).claim_batch(S.APPROVED, 1)
        assert held.id == top
        busy = await publish_now(top, now, env.manual, ADMIN)
    assert busy.outcome is ManualOutcome.BUSY
    assert len(env.telegram.sent) == sent_before
    assert await env.status(top) is S.APPROVED


async def test_publish_now_from_review_and_queue_cleanup(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    env = Env(session_factory)
    now = pipeline_now
    review = await env.post(0.7, now, status=S.PENDING_REVIEW)
    waiting = await env.post(0.6, now)

    result = await publish_now(review, now, env.manual, ADMIN)
    assert result.outcome is ManualOutcome.PUBLISHED
    assert await env.status(review) is S.PUBLISHED

    again = await publish_now(review, now, env.manual, ADMIN)
    assert again.outcome is ManualOutcome.ALREADY_DONE
    assert again.status is S.PUBLISHED
    assert len(env.telegram.sent) == 1

    assert await env.queue_ids() == [waiting]
