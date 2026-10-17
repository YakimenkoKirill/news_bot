from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Update
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import queue as queue_handlers
from app.bot.handlers.queue import QueueDeps
from app.bot.keyboards import QueueAction, QueueActionType, QueueTab
from app.bot.preview import PreviewDeps
from app.config import PublisherConfig, Settings
from app.db.base import session_scope
from app.db.models import NewsItem, ProcessedPost, Source
from app.db.repositories.misc import SettingsRepository
from app.db.repositories.news import NewsRepository, Priority
from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
from app.db.repositories.sources import SourceRepository
from app.domain.enums import NewsStatus, SourceType
from app.pipeline.review import ReviewDeps
from app.publisher.publisher import Publisher
from app.publisher.stage_publish import ManualPublishDeps, PublishDeps
from tests.bot_helpers import FakeSession, callback_update, make_bot
from tests.config_helpers import base_section, bot_config
from tests.fakes import FakeTelegramBot, RecordingSink, image_never_ok, no_sleep

pytestmark = pytest.mark.integration

ADMIN = 111
CHANNEL = -1001234567890
TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CFG = PublisherConfig(**{**base_section("publisher"), "quiet_hours": []})
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
    published_at: datetime | None = NOW
    raw_meta: Mapping[str, Any] = field(default_factory=dict)


class Env:
    """Обработчик /queue на реальной БД с фейковым ботом."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.counter = 0
        self.review = ReviewDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            post_repo=ProcessedPostRepository,
        )
        publish = PublishDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            post_repo=ProcessedPostRepository,
            source_repo=SourceRepository,
            publication_repo=PublicationRepository,
            settings_repo=SettingsRepository,
            check_image=image_never_ok,
            publisher=Publisher(FakeTelegramBot(), CHANNEL, CFG, sleep=no_sleep),
            sink=RecordingSink(),
            cfg=CFG,
            channel_id=CHANNEL,
            tz=TZ,
        )
        self.deps = QueueDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            post_repo=ProcessedPostRepository,
            publication_repo=PublicationRepository,
            settings_repo=SettingsRepository,
            source_repo=SourceRepository,
            preview=PreviewDeps(
                session_scope=self.scope,
                news_repo=NewsRepository,
                source_repo=SourceRepository,
                check_image=image_never_ok,
                cfg=CFG,
            ),
            publisher_cfg=CFG,
            channel_id=CHANNEL,
            tz=TZ,
            page_size=10,
            title_chars=bot_config().queue_title_chars,
            max_per_source=CFG.max_queued_per_source,
            clock=lambda: NOW,
            manual=ManualPublishDeps(publish=publish, news_repo=NewsRepository),
            review=self.review,
            batch_size=BATCH,
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
                    url=f"https://c.example/{self.counter}",
                    title=f"Новость {self.counter}",
                    text="текст новости",
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
        source = Source(type=SourceType.RSS, url="https://c.example/rss", name="C", interval_min=10)
        session.add(source)
        await session.flush()
        return source.id

    async def status(self, news_id: int) -> NewsStatus:
        async with self.scope() as session:
            news = await NewsRepository(session).get(news_id)
            assert news is not None
            return news.status

    async def due_reviews(self) -> list[int]:
        async with self.scope() as session:
            posts = await ProcessedPostRepository(session).list_due_reviews(NOW)
        return [post.news_item_id for post in posts]

    async def feed(self, update: Update) -> FakeSession:
        class NoAdmins:
            async def is_admin(self, telegram_id: int) -> bool:
                return False

        deps = BotDeps(
            settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
            admin_repo=NoAdmins(),
            routers=[queue_handlers.create_router()],
            workflow_data={"queue_deps": self.deps},
        )
        bot, session = make_bot()
        await create_dispatcher(deps).feed_update(bot, update)
        return session


def _cb(action: QueueActionType, news_id: int = 0) -> Update:
    data = QueueAction(action=action, tab=QueueTab.REVIEW, page=0, news_id=news_id).pack()
    return callback_update(ADMIN, data)


def _of(session: FakeSession, kind: type) -> list[Any]:
    return [r for r in session.requests if isinstance(r, kind)]


async def test_cancel_one_post_through_handler(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    news_id = await env.post(snoozed_until=NOW - timedelta(minutes=1))
    other = await env.post()

    session = await env.feed(_cb(QueueActionType.CONFIRM_CANCEL_REVIEW, news_id))

    assert await env.status(news_id) is S.REJECTED
    assert await env.status(other) is S.PENDING_REVIEW
    assert await env.due_reviews() == [], "отменённый пост не вернётся на модерацию"
    [edit] = _of(session, EditMessageText)
    assert texts.QUEUE_CANCELLED in (edit.text or "")


async def test_cancel_all_through_handler(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    pending = [await env.post() for _ in range(3)]
    approved = await env.post(status=S.APPROVED)

    session = await env.feed(_cb(QueueActionType.CONFIRM_CANCEL_ALL))

    for news_id in pending:
        assert await env.status(news_id) is S.REJECTED
    assert await env.status(approved) is S.APPROVED
    sent = [m.text for m in _of(session, SendMessage)]
    assert texts.QUEUE_CANCELLED_ALL.format(count=3) in sent
    assert any(texts.QUEUE_EMPTY["review"] in text for text in sent), "список снова пуст"


async def test_confirmation_shows_real_count(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    for _ in range(2):
        await env.post()
    await env.post(status=S.PUBLISHED)

    session = await env.feed(_cb(QueueActionType.CANCEL_ALL))

    [edit] = _of(session, EditMessageText)
    assert edit.text == texts.QUEUE_CANCEL_ALL_CONFIRM.format(count=2)


async def test_cancelled_post_is_not_published(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отменённый пост не выходит в канал: publish_due берёт только approved."""
    env = Env(session_factory)
    news_id = await env.post()
    await env.feed(_cb(QueueActionType.CONFIRM_CANCEL_REVIEW, news_id))

    async with env.scope() as session:
        rows = await NewsRepository(session).list_prepared(
            [S.APPROVED, S.SCHEDULED], Priority.from_config(CFG), limit=10, offset=0
        )
    assert rows == []
