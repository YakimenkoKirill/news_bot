import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import BotDeps, create_dispatcher
from app.bot.handlers import moderation as moderation_handlers
from app.bot.handlers.moderation import ModerationDeps
from app.bot.keyboards import ReviewAction, ReviewActionType
from app.collectors.health import SourceHealthTracker
from app.collectors.runner import CollectionDeps, run_collection_cycle
from app.config import (
    AiConfig,
    ChannelProfile,
    DedupConfig,
    FiltersConfig,
    PublisherConfig,
    Settings,
)
from app.db.base import session_scope
from app.db.models import NewsItem, ProcessedPost, Publication, Source
from app.db.repositories.misc import KeywordRepository, SettingsRepository
from app.db.repositories.news import NewsRepository
from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
from app.db.repositories.sources import SourceRepository
from app.domain.enums import ModerationMode, NewsStatus, PublicationStatus, SourceType
from app.pipeline.ai.budget import BudgetGuard
from app.pipeline.ai.processor import AiProcessor, AiRepos
from app.pipeline.stage_ai import AiStageDeps, process_normalized_batch
from app.pipeline.stage_normalize import NormalizeDeps, process_new_batch
from app.publisher.publisher import Publisher
from app.publisher.stage_publish import PublishDeps, publish_due
from tests.bot_helpers import callback_update, make_bot
from tests.config_helpers import base_section, bot_config, collectors_config, normalize_config
from tests.fakes import (
    FakeCollector,
    FakeDeepSeek,
    FakeTelegramBot,
    RecordingNotifier,
    RecordingSink,
    SessionCost,
    image_never_ok,
    no_sleep,
)

pytestmark = pytest.mark.integration

CHANNEL = -1001234567890
ADMIN = 111
TZ = ZoneInfo("Europe/Moscow")
TEXT = (
    "Совет директоров банка на заседании в пятницу принял решение сохранить ключевую ставку. "
    "Регулятор отметил, что инфляция замедляется, однако остаётся выше целевого уровня."
)
OTHER_TEXT = (
    "Городские власти открыли новую станцию метро на зелёной линии. Станция соединила новый "
    "жилой район с центром и сократила время в пути для жителей."
)


def _rate_news(now: datetime) -> dict[str, Any]:
    return _item("n1", "https://news.example/1", "Ставка сохранена", TEXT, now)


def _item(external_id: str, url: str, title: str, text: str, now: datetime) -> dict[str, Any]:
    return {
        "external_id": external_id,
        "url": url,
        "title": title,
        "text": text,
        "published_at": now - timedelta(hours=1),
    }


class Pipeline:
    """Все стадии пайплайна на реальной БД с фейковыми сборщиком, моделью и ботом."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        now: datetime,
        items_by_url: dict[str, list[dict[str, Any]]],
        *,
        daily_budget: str = "10",
    ) -> None:
        self.factory = factory
        self.now = now
        self.sink = RecordingSink()
        self.notifier = RecordingNotifier()
        self.deepseek = FakeDeepSeek()
        self.telegram = FakeTelegramBot()
        collectors_cfg = collectors_config()
        self.ai_cfg = AiConfig(
            **{
                **base_section("ai"),
                "daily_budget": daily_budget,
                "min_score": 0.5,
                "max_concurrency": 1,
            }
        )
        self.publisher_cfg = PublisherConfig(**{**base_section("publisher"), "quiet_hours": []})

        def health(session: AsyncSession) -> SourceHealthTracker:
            return SourceHealthTracker(SourceRepository(session), self.sink, collectors_cfg)

        self.collection = CollectionDeps(
            session_scope=self.scope,
            source_repo=SourceRepository,
            news_repo=NewsRepository,
            health=health,
            collectors={SourceType.RSS: FakeCollector(items_by_url)},
            max_parallel_sources=collectors_cfg.max_parallel_sources,
        )
        self.normalize = NormalizeDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            keyword_repo=KeywordRepository,
            savepoint=lambda session: session.begin_nested(),
            normalize=normalize_config(),
            filters=FiltersConfig(**{**base_section("filters"), "allowed_languages": ["ru"]}),
            dedup=DedupConfig(**base_section("dedup")),
            batch_size=20,
        )
        profile = ChannelProfile(**base_section("channel"))

        def processor(repos: AiRepos) -> AiProcessor:
            return AiProcessor(self.deepseek, profile, repos, self.ai_cfg)

        self.ai = AiStageDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            source_repo=SourceRepository,
            post_repo=ProcessedPostRepository,
            settings_repo=SettingsRepository,
            processor=processor,
            budget=BudgetGuard(SessionCost(self.scope), self.sink, self.ai_cfg, TZ),
            notifier=self.notifier,
            sink=self.sink,
            batch_size=20,
            max_concurrency=self.ai_cfg.max_concurrency,
        )
        self.publish = PublishDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            post_repo=ProcessedPostRepository,
            source_repo=SourceRepository,
            publication_repo=PublicationRepository,
            settings_repo=SettingsRepository,
            check_image=image_never_ok,
            publisher=Publisher(self.telegram, CHANNEL, self.publisher_cfg, sleep=no_sleep),
            sink=self.sink,
            cfg=self.publisher_cfg,
            channel_id=CHANNEL,
            tz=TZ,
        )

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    async def add_source(self, url: str, mode: ModerationMode = ModerationMode.AUTO) -> int:
        async with self.scope() as session:
            source = await SourceRepository(session).add(
                Source(
                    type=SourceType.RSS, url=url, name=url, interval_min=10, moderation_mode=mode
                )
            )
            return source.id

    async def collect_and_normalize(self) -> None:
        await run_collection_cycle(self.now, self.collection)
        await process_new_batch(self.now, self.normalize)

    async def run_ai(self) -> None:
        await process_normalized_batch(self.now, self.ai)

    async def statuses(self) -> dict[str, NewsStatus]:
        async with self.scope() as session:
            rows = (await session.execute(select(NewsItem.external_id, NewsItem.status))).all()
        return {external_id: status for external_id, status in rows}

    async def publications(self) -> list[Publication]:
        async with self.scope() as session:
            return list((await session.scalars(select(Publication))).all())


async def test_rss_news_reaches_published(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    url = "https://news.example/rss"
    pipe = Pipeline(session_factory, pipeline_now, {url: [_rate_news(pipeline_now)]})
    await pipe.add_source(url)
    await pipe.collect_and_normalize()
    assert await pipe.statuses() == {"n1": NewsStatus.NORMALIZED}
    await pipe.run_ai()
    assert await pipe.statuses() == {"n1": NewsStatus.APPROVED}
    report = await publish_due(pipeline_now, pipe.publish)
    assert report.message_id == 1001
    assert await pipe.statuses() == {"n1": NewsStatus.PUBLISHED}
    [publication] = await pipe.publications()
    assert publication.status is PublicationStatus.SENT
    assert publication.message_id == 1001
    assert publication.channel_id == CHANNEL
    [sent] = pipe.telegram.sent
    assert sent["chat_id"] == CHANNEL
    assert "Пересказ главной новости" in sent["text"]
    assert "news.example" not in sent["text"]


async def test_same_news_from_second_source_is_duplicate(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    first, second = "https://a.example/rss", "https://b.example/rss"
    pipe = Pipeline(
        session_factory,
        pipeline_now,
        {
            first: [_item("a1", "https://a.example/1", "Ставка сохранена", TEXT, pipeline_now)],
            second: [
                _item("b1", "https://b.example/копия", "Ставка сохранена", TEXT, pipeline_now)
            ],
        },
    )
    await pipe.add_source(first)
    await pipe.add_source(second)
    await pipe.collect_and_normalize()
    statuses = await pipe.statuses()
    assert sorted(statuses.values()) == sorted([NewsStatus.NORMALIZED, NewsStatus.DUPLICATE])
    async with pipe.scope() as session:
        items = list((await session.scalars(select(NewsItem))).all())
    # Источники собираются параллельно: оригиналом может оказаться любая из двух новостей.
    original = next(i for i in items if i.status is NewsStatus.NORMALIZED)
    duplicate = next(i for i in items if i.status is NewsStatus.DUPLICATE)
    assert duplicate.cluster_id == original.id
    assert (duplicate.reject_reason or "").startswith("duplicate:hash:")


async def test_irrelevant_news_filtered(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    url = "https://news.example/rss"
    now = pipeline_now
    pipe = Pipeline(
        session_factory,
        pipeline_now,
        {url: [_item("n1", "https://news.example/1", "Нерелевантная заметка", OTHER_TEXT, now)]},
    )
    await pipe.add_source(url)
    await pipe.collect_and_normalize()
    await pipe.run_ai()
    assert await pipe.statuses() == {"n1": NewsStatus.FILTERED}
    async with pipe.scope() as session:
        item = (await session.scalars(select(NewsItem))).one()
        post_count = await session.scalar(select(func.count()).select_from(ProcessedPost))
    assert item.reject_reason == "not_relevant"
    assert post_count == 1
    assert (await publish_due(pipeline_now, pipe.publish)).skipped == "empty"


async def test_review_mode_published_after_button(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    url = "https://news.example/rss"
    pipe = Pipeline(session_factory, pipeline_now, {url: [_rate_news(pipeline_now)]})
    await pipe.add_source(url, ModerationMode.REVIEW)
    await pipe.collect_and_normalize()
    await pipe.run_ai()
    assert await pipe.statuses() == {"n1": NewsStatus.PENDING_REVIEW}
    [post] = pipe.notifier.posts
    assert (await publish_due(pipeline_now, pipe.publish)).skipped == "empty"

    dispatcher = create_dispatcher(
        BotDeps(
            settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
            admin_repo=_NoDbAdmins(),
            routers=[moderation_handlers.create_router()],
            workflow_data={
                "moderation_deps": ModerationDeps(
                    session_scope=pipe.scope,
                    news_repo=NewsRepository,
                    post_repo=ProcessedPostRepository,
                    source_repo=SourceRepository,
                    cfg=pipe.publisher_cfg,
                    snooze_min=60,
                    clock=lambda: pipeline_now,
                )
            },
        )
    )
    bot, _ = make_bot()
    data = ReviewAction(action=ReviewActionType.PUBLISH, post_id=post.id).pack()
    await dispatcher.feed_update(bot, callback_update(ADMIN, data))
    assert await pipe.statuses() == {"n1": NewsStatus.APPROVED}

    report = await publish_due(pipeline_now, pipe.publish)
    assert report.message_id is not None
    assert await pipe.statuses() == {"n1": NewsStatus.PUBLISHED}


class _NoDbAdmins:
    async def is_admin(self, telegram_id: int) -> bool:
        return False


async def test_budget_exceeded_keeps_news_normalized(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    url = "https://news.example/rss"
    items = [
        _item("n1", "https://news.example/1", "Ставка сохранена", TEXT, pipeline_now),
        _item("n2", "https://news.example/2", "Метро открыли", OTHER_TEXT, pipeline_now),
    ]
    pipe = Pipeline(session_factory, pipeline_now, {url: items}, daily_budget="0.000001")
    await pipe.add_source(url)
    await pipe.collect_and_normalize()
    await pipe.run_ai()
    statuses = await pipe.statuses()
    assert sorted(statuses.values()) == sorted([NewsStatus.APPROVED, NewsStatus.NORMALIZED])
    assert len(pipe.deepseek.calls) == 1
    assert any(event.kind.value == "budget_exceeded" for event in pipe.sink.events)


async def test_parallel_publish_does_not_duplicate(
    session_factory: async_sessionmaker[AsyncSession], pipeline_now: datetime
) -> None:
    url = "https://news.example/rss"
    pipe = Pipeline(session_factory, pipeline_now, {url: [_rate_news(pipeline_now)]})
    await pipe.add_source(url)
    await pipe.collect_and_normalize()
    await pipe.run_ai()
    reports = await asyncio.gather(
        publish_due(pipeline_now, pipe.publish), publish_due(pipeline_now, pipe.publish)
    )
    published = [r for r in reports if r.message_id is not None]
    assert len(published) == 1
    assert len(pipe.telegram.sent) == 1
    assert len(await pipe.publications()) == 1
    assert await pipe.statuses() == {"n1": NewsStatus.PUBLISHED}
