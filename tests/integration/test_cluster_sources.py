import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, pool, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import DedupConfig, FiltersConfig, PublisherConfig
from app.db.base import session_scope
from app.db.models import NewsItem, ProcessedPost, Source
from app.db.repositories.misc import KeywordRepository
from app.db.repositories.news import NewsRepository, Priority
from app.domain.enums import NewsStatus, SourceType
from app.pipeline.stage_normalize import NormalizeDeps, process_new_batch
from tests.config_helpers import base_section, normalize_config

pytestmark = pytest.mark.integration

T = TypeVar("T")

S = NewsStatus
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
QUEUE = [S.APPROVED, S.SCHEDULED]
CFG = PublisherConfig(**base_section("publisher"))
PRIORITY = Priority.from_config(CFG)
TEXT = (
    "Совет директоров Банка России принял решение сохранить ключевую ставку. "
    "Регулятор отметил, что инфляция замедляется, но остаётся выше цели."
)
OTHER_TEXT = (
    "Городские власти открыли новую станцию метро на зелёной линии. "
    "Станция соединила жилой район с центром города."
)


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


class Fixture:
    """Новости нескольких источников и прогон нормализации на реальной БД."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.counter = 0
        self.deps = NormalizeDeps(
            session_scope=self.scope,
            news_repo=NewsRepository,
            keyword_repo=KeywordRepository,
            savepoint=lambda session: session.begin_nested(),
            normalize=normalize_config(),
            filters=FiltersConfig(**{**base_section("filters"), "allowed_languages": ["ru"]}),
            dedup=DedupConfig(**base_section("dedup")),
            batch_size=20,
        )

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    async def source(self, name: str) -> int:
        async with self.scope() as session:
            source = Source(
                type=SourceType.RSS,
                url=f"https://{name}.example/rss",
                name=name,
                interval_min=10,
            )
            session.add(source)
            await session.flush()
            return source.id

    async def news(
        self,
        source_id: int,
        body: str = TEXT,
        *,
        title: str = "Центробанк сохранил ключевую ставку",
        status: NewsStatus = S.NEW,
    ) -> int:
        async with self.scope() as session:
            self.counter += 1
            item = await NewsRepository(session).add_if_new(
                Raw(
                    source_id=source_id,
                    external_id=f"{source_id}-{self.counter}",
                    url=f"https://{source_id}.example/{self.counter}",
                    title=title,
                    text=body,
                )
            )
            assert item is not None
            if status is not S.NEW:
                await session.execute(
                    update(NewsItem).where(NewsItem.id == item.id).values(status=status)
                )
            return item.id

    async def normalize(self) -> None:
        await process_new_batch(NOW, self.deps)

    async def row(self, news_id: int) -> NewsItem:
        async with self.scope() as session:
            item = await NewsRepository(session).get(news_id)
            assert item is not None
            return item

    async def approve(self, news_id: int, score: float) -> None:
        """Доводит новость до очереди публикации с пересказом."""
        async with self.scope() as session:
            session.add(
                ProcessedPost(
                    news_item_id=news_id,
                    ai_title="Заголовок",
                    ai_summary="Пересказ новости.",
                    hashtags=[],
                    relevance_score=score,
                    prompt_version="v4",
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost=Decimal("0.000001"),
                )
            )
            await session.execute(
                update(NewsItem).where(NewsItem.id == news_id).values(status=S.APPROVED)
            )

    async def queue_order(self) -> list[int]:
        async with self.scope() as session:
            rows = await NewsRepository(session).list_prepared(QUEUE, PRIORITY, limit=100, offset=0)
        return [news.id for news, _ in rows]


async def test_duplicate_from_other_source_raises_counter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    first_source = await fixture.source("a")
    second_source = await fixture.source("b")
    root = await fixture.news(first_source)
    await fixture.normalize()
    assert (await fixture.row(root)).cluster_sources == 1

    duplicate = await fixture.news(second_source)
    await fixture.normalize()

    assert (await fixture.row(duplicate)).status is S.DUPLICATE
    assert (await fixture.row(duplicate)).cluster_id == root
    assert (await fixture.row(root)).cluster_sources == 2


async def test_duplicate_from_same_source_keeps_counter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("a")
    root = await fixture.news(source_id)
    await fixture.normalize()

    duplicate = await fixture.news(source_id)
    await fixture.normalize()

    assert (await fixture.row(duplicate)).status is S.DUPLICATE
    assert (await fixture.row(root)).cluster_sources == 1


async def test_chain_of_duplicates_counted_on_root(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дубль дубля попадает в тот же кластер: счётчик растёт у корня."""
    fixture = Fixture(session_factory)
    sources = [await fixture.source(name) for name in ("a", "b", "c")]
    root = await fixture.news(sources[0])
    await fixture.normalize()
    second = await fixture.news(sources[1])
    await fixture.normalize()
    third = await fixture.news(sources[2])
    await fixture.normalize()

    assert (await fixture.row(second)).cluster_id == root
    assert (await fixture.row(third)).cluster_id == root
    assert (await fixture.row(root)).cluster_sources == 3
    assert (await fixture.row(third)).cluster_sources == 1, "счётчик живёт у корня"


async def test_counter_grows_for_published_root(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    first_source = await fixture.source("a")
    second_source = await fixture.source("b")
    root = await fixture.news(first_source)
    await fixture.normalize()
    async with fixture.scope() as session:
        await session.execute(
            update(NewsItem).where(NewsItem.id == root).values(status=S.PUBLISHED)
        )

    await fixture.news(second_source)
    await fixture.normalize()

    assert (await fixture.row(root)).cluster_sources == 2
    assert (await fixture.row(root)).status is S.PUBLISHED


async def test_unrelated_news_do_not_share_counter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    first_source = await fixture.source("a")
    second_source = await fixture.source("b")
    rate = await fixture.news(first_source)
    metro = await fixture.news(
        second_source, OTHER_TEXT, title="В городе открыли новую станцию метро"
    )
    await fixture.normalize()

    assert (await fixture.row(rate)).cluster_sources == 1
    assert (await fixture.row(metro)).cluster_sources == 1
    assert (await fixture.row(metro)).status is S.NORMALIZED


async def test_queue_order_prefers_more_sources(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Одинаковая оценка и возраст: раньше выходит событие с двумя источниками."""
    fixture = Fixture(session_factory)
    first_source = await fixture.source("a")
    second_source = await fixture.source("b")
    single = await fixture.news(
        first_source, OTHER_TEXT, title="В городе открыли новую станцию метро"
    )
    shared = await fixture.news(first_source)
    await fixture.normalize()
    await fixture.news(second_source)
    await fixture.normalize()

    await fixture.approve(single, 0.7)
    await fixture.approve(shared, 0.7)

    assert await fixture.queue_order() == [shared, single]
    async with fixture.scope() as session:
        repo = NewsRepository(session)
        with_bonus = await repo.priority_of(shared, PRIORITY)
        without_bonus = await repo.priority_of(single, PRIORITY)
        assert with_bonus is not None and without_bonus is not None
        assert with_bonus - without_bonus == pytest.approx(CFG.cluster_bonus)

        no_bonus = Priority(decay_per_h=CFG.score_decay_per_h)
        assert await repo.priority_of(shared, no_bonus) == await repo.priority_of(
            single, no_bonus
        ), "при cluster_bonus = 0 порядок как раньше"


async def test_bonus_is_capped(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """10 источников при cluster_bonus_max = 3 дают бонус как за 4 источника."""
    fixture = Fixture(session_factory)
    source_id = await fixture.source("a")
    news_id = await fixture.news(source_id)
    await fixture.normalize()
    await fixture.approve(news_id, 0.5)

    async with fixture.scope() as session:
        repo = NewsRepository(session)
        base = await repo.priority_of(news_id, PRIORITY)
        assert base is not None

    priorities: dict[int, float] = {}
    for sources in (4, 11):
        async with fixture.scope() as session:
            await session.execute(
                update(NewsItem).where(NewsItem.id == news_id).values(cluster_sources=sources)
            )
        async with fixture.scope() as session:
            value = await NewsRepository(session).priority_of(news_id, PRIORITY)
            assert value is not None
            priorities[sources] = value

    assert priorities[4] == pytest.approx(base + CFG.cluster_bonus * CFG.cluster_bonus_max)
    assert priorities[11] == pytest.approx(priorities[4])


async def test_claim_next_follows_bonus(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("a")
    weak = await fixture.news(source_id, OTHER_TEXT, title="Метро")
    strong = await fixture.news(source_id, TEXT)
    await fixture.normalize()
    await fixture.approve(weak, 0.8)
    await fixture.approve(strong, 0.8)
    async with fixture.scope() as session:
        await session.execute(
            update(NewsItem).where(NewsItem.id == strong).values(cluster_sources=3)
        )

    async with session_factory() as session, session.begin():
        claimed = await NewsRepository(session).claim_next(QUEUE, PRIORITY)
        assert claimed is not None
        assert claimed.id == strong


def _with_connection(url: str, fn: Callable[[Connection], T]) -> T:
    """Синхронный доступ к БД вне рабочего цикла событий (как в test_migrations)."""

    async def run() -> T:
        engine = create_async_engine(url, poolclass=pool.NullPool)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(fn)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _seed_cluster(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO sources (type, url, name, interval_min) VALUES "
            "('rss', 'https://c1.example/rss', 'c1', 10), "
            "('rss', 'https://c2.example/rss', 'c2', 10)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO news_items (id, source_id, external_id, url, title, raw_text, "
            "status, cluster_id) VALUES "
            "(1, 1, '1', 'https://c1.example/1', 't', 'r', 'published', 1), "
            "(2, 2, '2', 'https://c2.example/2', 't', 'r', 'duplicate', 1), "
            "(3, 1, '3', 'https://c1.example/3', 't', 'r', 'duplicate', 1), "
            "(4, 2, '4', 'https://c2.example/4', 't', 'r', 'normalized', NULL)"
        )
    )
    connection.commit()


def _counters(connection: Connection) -> list[tuple[int, int]]:
    rows = connection.execute(text("SELECT id, cluster_sources FROM news_items ORDER BY id"))
    return [(int(row[0]), int(row[1])) for row in rows.all()]


def _columns(connection: Connection) -> set[str]:
    return {str(column["name"]) for column in inspect(connection).get_columns("news_items")}


def test_migration_fills_cluster_sources(alembic_config: Config, test_database_url: str) -> None:
    """Миграция считает разные источники существующих кластеров."""
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "0007")
    _with_connection(test_database_url, _seed_cluster)

    command.upgrade(alembic_config, "head")

    assert _with_connection(test_database_url, _counters) == [(1, 2), (2, 1), (3, 1), (4, 1)]
    assert "cluster_sources" in _with_connection(test_database_url, _columns)

    command.downgrade(alembic_config, "0007")
    assert "cluster_sources" not in _with_connection(test_database_url, _columns)
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")


async def test_queue_order_unchanged_without_bonus(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """cluster_bonus = 0: порядок по оценке и свежести, как до шага."""
    fixture = Fixture(session_factory)
    source_id = await fixture.source("a")
    shared = await fixture.news(source_id, TEXT)
    other = await fixture.news(source_id, OTHER_TEXT, title="Метро")
    await fixture.normalize()
    await fixture.approve(shared, 0.5)
    await fixture.approve(other, 0.9)
    async with fixture.scope() as session:
        await session.execute(
            update(NewsItem).where(NewsItem.id == shared).values(cluster_sources=4)
        )

    async with fixture.scope() as session:
        rows = await NewsRepository(session).list_prepared(
            QUEUE, Priority(decay_per_h=CFG.score_decay_per_h), limit=10, offset=0
        )
    assert [news.id for news, _ in rows] == [other, shared]


async def test_counter_default_is_one(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("a")
    news_id = await fixture.news(source_id)

    async with fixture.scope() as session:
        stored = await session.scalar(
            select(NewsItem.cluster_sources).where(NewsItem.id == news_id)
        )
    assert stored == 1
    assert (NOW - timedelta(hours=1)) < NOW
