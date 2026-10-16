from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import PublisherConfig
from app.db.base import session_scope
from app.db.models import NewsItem, ProcessedPost, Publication, Source
from app.db.repositories.misc import SettingsRepository
from app.db.repositories.news import NewsRepository
from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
from app.db.repositories.sources import SourceRepository
from app.domain.alerts import AlertKind
from app.domain.enums import NewsStatus, PublicationStatus, SourceType
from app.publisher.publisher import Publisher
from app.publisher.stage_publish import (
    REASON_PUBLISH_UNKNOWN,
    PublishDeps,
    publish_due,
    recover_stuck,
)
from tests.config_helpers import base_section
from tests.fakes import FakeTelegramBot, RecordingSink, image_never_ok, no_sleep

pytestmark = pytest.mark.integration

S = NewsStatus
CHANNEL = -1001234567890
TZ = ZoneInfo("Europe/Moscow")
CFG = PublisherConfig(**{**base_section("publisher"), "quiet_hours": []})
STUCK_AFTER = timedelta(minutes=CFG.stuck_after_min)


class CrashingPublications(PublicationRepository):
    """Падает на записи успешной публикации: обрыв после отправки в канал."""

    async def add(self, publication: Publication) -> Publication:
        if publication.status is PublicationStatus.SENT:
            raise RuntimeError("соединение с БД потеряно")
        return await super().add(publication)


class Env:
    """Публикация на реальной БД с фейковым ботом Telegram."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.telegram = FakeTelegramBot()
        self.sink = RecordingSink()
        self.publish = PublishDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            post_repo=ProcessedPostRepository,
            source_repo=SourceRepository,
            publication_repo=PublicationRepository,
            settings_repo=SettingsRepository,
            check_image=image_never_ok,
            publisher=Publisher(self.telegram, CHANNEL, CFG, sleep=no_sleep),
            sink=self.sink,
            cfg=CFG,
            channel_id=CHANNEL,
            tz=TZ,
        )
        self._source_id: int | None = None
        self._counter = 0

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    async def post(self, status: NewsStatus = S.APPROVED, score: float = 0.9) -> int:
        async with self.scope() as session:
            if self._source_id is None:
                source = Source(
                    type=SourceType.RSS, url="https://p.example/rss", name="P", interval_min=10
                )
                session.add(source)
                await session.flush()
                self._source_id = source.id
            self._counter += 1
            news = NewsItem(
                source_id=self._source_id,
                external_id=str(self._counter),
                url=f"https://p.example/{self._counter}",
                title=f"Новость {self._counter}",
                raw_text="текст",
                status=status,
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

    async def add_sent_publication(self, news_id: int, now: datetime) -> None:
        async with self.scope() as session:
            post = await ProcessedPostRepository(session).get_by_news(news_id)
            assert post is not None
            await PublicationRepository(session).add(
                Publication(
                    processed_post_id=post.id,
                    channel_id=CHANNEL,
                    status=PublicationStatus.SENT,
                    message_id=777,
                    published_at=now,
                )
            )

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

    async def publications(self) -> list[Publication]:
        async with self.scope() as session:
            rows = await session.scalars(select(Publication).order_by(Publication.id))
            return list(rows)

    def crashing(self) -> PublishDeps:
        """Те же зависимости, но запись успешной публикации падает."""
        return replace(self.publish, publication_repo=CrashingPublications)


async def test_publication_does_not_hold_transaction(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    """Пост уходит в канал, статус меняется, запись о публикации появляется."""
    env = Env(session_factory)
    news_id = await env.post()

    report = await publish_due(pipeline_now, env.publish)

    assert report.news_id == news_id
    assert report.message_id is not None
    assert await env.status(news_id) is S.PUBLISHED
    [publication] = await env.publications()
    assert publication.status is PublicationStatus.SENT
    assert len(env.telegram.sent) == 1


async def test_crash_after_send_leaves_post_scheduled_and_recover_warns(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    """Падение между отправкой и записью итога: повторной публикации нет, админ предупреждён."""
    env = Env(session_factory)
    news_id = await env.post()

    with pytest.raises(RuntimeError):
        await publish_due(pipeline_now, env.crashing())

    assert len(env.telegram.sent) == 1
    assert await env.status(news_id) is S.SCHEDULED
    assert await env.publications() == []

    # Пост в scheduled уже не захватывается: автоматически второй раз он не выйдет.
    assert (await publish_due(pipeline_now, env.publish)).skipped == "empty"
    assert len(env.telegram.sent) == 1

    # Свежую отправку восстановление не трогает.
    assert await recover_stuck(pipeline_now, env.publish) == 0
    assert await env.status(news_id) is S.SCHEDULED

    later = pipeline_now + STUCK_AFTER + timedelta(minutes=1)
    assert await recover_stuck(later, env.publish) == 1
    assert await env.status(news_id) is S.FAILED
    assert await env.reason(news_id) == REASON_PUBLISH_UNKNOWN
    [event] = env.sink.events
    assert event.kind is AlertKind.PUBLISH_ERROR
    assert "Проверьте канал" in event.message
    assert len(env.telegram.sent) == 1, "восстановление не отправляет пост заново"


async def test_recover_publishes_post_with_sent_publication(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    """Запись Publication(sent) есть — пост дошёл до канала, статус догоняет."""
    env = Env(session_factory)
    news_id = await env.post(status=S.SCHEDULED)
    await env.add_sent_publication(news_id, pipeline_now)

    later = pipeline_now + STUCK_AFTER + timedelta(minutes=1)
    assert await recover_stuck(later, env.publish) == 1
    assert await env.status(news_id) is S.PUBLISHED
    assert env.sink.events == []
    assert env.telegram.sent == []


async def test_recover_keeps_other_statuses(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    env = Env(session_factory)
    approved = await env.post()
    scheduled = await env.post(status=S.SCHEDULED)

    later = pipeline_now + STUCK_AFTER + timedelta(minutes=1)
    assert await recover_stuck(later, env.publish) == 1
    assert await env.status(approved) is S.APPROVED
    assert await env.status(scheduled) is S.FAILED
