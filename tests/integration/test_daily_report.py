from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import AiRequest, Incident, NewsItem, ProcessedPost, Publication, Source
from app.domain.alerts import AlertKind
from app.domain.enums import NewsStatus, PublicationStatus, SourceType
from app.reports.daily import DailyContext, collect_daily, post_url
from app.reports.metrics import Period

pytestmark = pytest.mark.integration

S = NewsStatus
MSK = ZoneInfo("Europe/Moscow")
CHANNEL = -1001234567890
ADMIN = 111
TODAY = date(2026, 10, 14)
PERIOD = Period.day(TODAY, MSK)
PREVIOUS = Period.day(TODAY - timedelta(days=1), MSK)
CTX = DailyContext(
    tz=MSK, daily_budget=Decimal("0.5"), decay_per_h=0.02, top_posts=3, channel_id=CHANNEL
)


def msk(hour: int, minute: int = 0, day: int = 14) -> datetime:
    """Московское время 2026-10-<day> в UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=MSK).astimezone(UTC)


class Fixture:
    """Наполняет базу новостями, пересказами и публикациями."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.counter = 0

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    async def source(self, name: str, enabled: bool = True, errors: int = 0) -> int:
        async with self.scope() as session:
            source = Source(
                type=SourceType.RSS,
                url=f"https://{name}.example/rss",
                name=name,
                interval_min=10,
                enabled=enabled,
                error_count=errors,
            )
            session.add(source)
            await session.flush()
            return source.id

    async def news(
        self,
        source_id: int,
        *,
        status: NewsStatus = S.PUBLISHED,
        created_at: datetime,
        published_at: datetime | None = None,
        reason: str | None = None,
        score: float | None = None,
        cost: str = "0.001",
        post_created_at: datetime | None = None,
        edited_by: int | None = None,
        tokens: tuple[int, int] = (100, 50),
        cached_tokens: int = 0,
        title: str = "Заголовок",
    ) -> tuple[int, int | None]:
        """Создаёт новость и, если задан score, пересказ. Возвращает (news_id, post_id)."""
        async with self.scope() as session:
            self.counter += 1
            item = NewsItem(
                source_id=source_id,
                external_id=str(self.counter),
                url=f"https://e.example/{self.counter}",
                title=f"Новость {self.counter}",
                raw_text="текст",
                status=status,
                reject_reason=reason,
                created_at=created_at,
                published_at=published_at,
            )
            session.add(item)
            await session.flush()
            post_id = None
            if score is not None:
                post = ProcessedPost(
                    news_item_id=item.id,
                    ai_title=title,
                    ai_summary="Пересказ новости.",
                    hashtags=[],
                    relevance_score=score,
                    prompt_version="v3",
                    prompt_tokens=tokens[0],
                    completion_tokens=tokens[1],
                    cost=Decimal(cost),
                    edited_by=edited_by,
                    created_at=post_created_at or created_at,
                )
                session.add(post)
                await session.flush()
                post_id = post.id
                session.add(
                    AiRequest(
                        news_item_id=item.id,
                        kind="summary",
                        model="test-model",
                        prompt_tokens=tokens[0],
                        cached_tokens=cached_tokens,
                        completion_tokens=tokens[1],
                        cost=Decimal(cost),
                        ok=True,
                        created_at=post_created_at or created_at,
                    )
                )
            return item.id, post_id

    async def ai_request(
        self,
        news_id: int | None,
        *,
        created_at: datetime,
        cost: str = "0.001",
        ok: bool = True,
        tokens: tuple[int, int] = (100, 50),
        cached_tokens: int = 0,
        kind: str = "summary",
    ) -> None:
        """Добавляет запрос к модели без пересказа (например, неудачную попытку)."""
        async with self.scope() as session:
            session.add(
                AiRequest(
                    news_item_id=news_id,
                    kind=kind,
                    model="test-model",
                    prompt_tokens=tokens[0],
                    cached_tokens=cached_tokens,
                    completion_tokens=tokens[1],
                    cost=Decimal(cost),
                    ok=ok,
                    created_at=created_at,
                )
            )

    async def publish(
        self,
        post_id: int,
        at: datetime | None,
        *,
        status: PublicationStatus = PublicationStatus.SENT,
        initiated_by: int | None = None,
        message_id: int | None = None,
        views: int | None = None,
        forwards: int | None = None,
        reactions: int | None = None,
        stats_at: datetime | None = None,
    ) -> None:
        async with self.scope() as session:
            session.add(
                Publication(
                    processed_post_id=post_id,
                    channel_id=CHANNEL,
                    status=status,
                    message_id=message_id,
                    published_at=at,
                    initiated_by=initiated_by,
                    error=None if status is PublicationStatus.SENT else "Telegram отклонил",
                    views=views,
                    forwards=forwards,
                    reactions=reactions,
                    stats_at=stats_at if stats_at is not None or views is None else at,
                )
            )

    async def incident(self, kind: AlertKind, first_seen_at: datetime, resolved: bool) -> None:
        async with self.scope() as session:
            session.add(
                Incident(
                    kind=kind.value,
                    key=f"{kind.value}:{self.counter}",
                    message="сбой",
                    count=1,
                    first_seen_at=first_seen_at,
                    last_seen_at=first_seen_at,
                    resolved_at=first_seen_at if resolved else None,
                    resolved_by=ADMIN if resolved else None,
                )
            )
            self.counter += 1

    async def report(self, session: AsyncSession, paused: bool = False):  # type: ignore[no-untyped-def]
        return await collect_daily(session, PERIOD, PREVIOUS, CTX, paused=paused)


def test_post_url_strips_channel_prefix() -> None:
    assert post_url(CHANNEL, 42) == "https://t.me/c/1234567890/42"
    assert post_url(CHANNEL, None) is None


async def test_empty_day_has_zeros_and_none(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    report = await collect_daily(session, PERIOD, PREVIOUS, CTX)
    assert report.publications.total == 0
    assert report.publications.top == ()
    assert report.sources == ()
    assert report.funnel.collected == 0
    assert report.spending.cost == Decimal(0)
    assert report.spending.cost_per_post is None
    assert report.quality.delay_median_min is None
    assert report.quality.avg_published_score is None
    assert report.queue.best_title is None
    assert report.errors.open_incidents == 0
    assert report.previous.publications == 0


async def test_publications_counted_by_kind(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")

    _, scheduled = await fixture.news(source_id, created_at=msk(9), score=0.9, title="Плановый")
    _, manual = await fixture.news(source_id, created_at=msk(10), score=0.7, title="Ручной")
    _, edited = await fixture.news(
        source_id, created_at=msk(11), score=0.5, title="Правленый", edited_by=ADMIN
    )
    assert scheduled and manual and edited
    await fixture.publish(scheduled, msk(12), message_id=11)
    await fixture.publish(manual, msk(13), initiated_by=ADMIN, message_id=12)
    await fixture.publish(edited, msk(13, 30), message_id=13)
    await fixture.publish(edited, None, status=PublicationStatus.FAILED)

    report = await fixture.report(session)
    pubs = report.publications
    assert (pubs.total, pubs.scheduled, pubs.manual, pubs.edited, pubs.failed) == (3, 2, 1, 1, 1)
    assert pubs.by_hour == {12: 1, 13: 2}
    assert [p.title for p in pubs.top] == ["Плановый", "Ручной", "Правленый"]
    assert pubs.top[0].url == "https://t.me/c/1234567890/11"
    assert pubs.top[0].source == "A"


async def test_top_limited_by_config(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    for index in range(5):
        _, post_id = await fixture.news(source_id, created_at=msk(9), score=0.1 * (index + 1))
        assert post_id
        await fixture.publish(post_id, msk(12), message_id=index)

    report = await fixture.report(session)
    assert len(report.publications.top) == CTX.top_posts
    scores = [p.score for p in report.publications.top]
    assert scores == sorted(scores, reverse=True)


async def test_funnel_and_sources(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    first = await fixture.source("A", errors=2)
    second = await fixture.source("B", enabled=False)

    _, published = await fixture.news(
        first, created_at=msk(8), published_at=msk(7), score=0.9, cost="0.002"
    )
    assert published
    await fixture.publish(published, msk(9), message_id=1)
    await fixture.news(first, status=S.DUPLICATE, created_at=msk(8), reason="duplicate:url:1")
    await fixture.news(first, status=S.DUPLICATE, created_at=msk(8), reason="duplicate:simhash:2")
    await fixture.news(first, status=S.FILTERED, created_at=msk(8), reason="too_old")
    await fixture.news(first, status=S.FILTERED, created_at=msk(8), reason="language:en")
    await fixture.news(
        first, status=S.FILTERED, created_at=msk(8), reason="not_relevant", score=0.1
    )
    await fixture.news(first, status=S.FAILED, created_at=msk(8), reason="bad_ai_response")
    await fixture.news(second, status=S.PENDING_REVIEW, created_at=msk(8), score=0.6)
    await fixture.news(second, status=S.REJECTED, created_at=msk(8), score=0.4)
    await fixture.news(second, status=S.APPROVED, created_at=msk(8), score=0.8)

    report = await fixture.report(session)
    funnel = report.funnel
    assert funnel.collected == 10
    assert funnel.duplicates == 2
    assert funnel.duplicates_by_level == {"url": 1, "simhash": 1}
    assert funnel.filtered == 2
    assert funnel.filtered_by_reason == {"too_old": 1, "language": 1}
    assert funnel.not_relevant == 1
    assert funnel.ai_failed == 1
    assert funnel.pending_review == 1
    assert funnel.rejected == 1
    assert funnel.in_queue == 1
    assert funnel.published == 1
    assert funnel.processed == 5

    by_name = {item.name: item for item in report.sources}
    assert [item.name for item in report.sources] == ["A", "B"]
    assert by_name["A"].published == 1
    assert by_name["A"].collected == 7
    assert by_name["A"].duplicates == 2
    assert by_name["A"].filtered == 2
    assert by_name["A"].not_relevant == 1
    assert by_name["A"].ai_failed == 1
    assert by_name["A"].error_count == 2
    assert by_name["A"].enabled is True
    assert by_name["B"].rejected == 1
    assert by_name["B"].enabled is False
    assert by_name["B"].published == 0


async def test_spending_and_quality(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    _, first = await fixture.news(
        source_id,
        created_at=msk(9),
        published_at=msk(8),
        score=0.9,
        cost="0.002",
        tokens=(100, 50),
    )
    _, second = await fixture.news(
        source_id,
        created_at=msk(10),
        published_at=msk(9),
        score=0.5,
        cost="0.003",
        tokens=(200, 70),
    )
    assert first and second
    await fixture.publish(first, msk(10), message_id=1)
    await fixture.publish(second, msk(11), message_id=2)

    report = await fixture.report(session)
    spending = report.spending
    assert spending.cost == Decimal("0.005")
    assert spending.requests == 2
    assert (spending.prompt_tokens, spending.completion_tokens) == (300, 120)
    assert spending.budget_share == pytest.approx(0.01)
    assert spending.cost_per_post == Decimal("0.005") / 2
    assert spending.month_to_date == Decimal("0.005")

    quality = report.quality
    assert quality.delay_median_min == pytest.approx(120.0)
    assert quality.avg_published_score == pytest.approx(0.7)
    assert quality.duplicate_share == 0.0


async def test_failed_requests_counted_in_spending(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Две неудачные попытки без пересказа всё равно видны в расходе."""
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    news_id, post_id = await fixture.news(
        source_id, created_at=msk(9), published_at=msk(8), score=0.9, cost="0.002"
    )
    assert post_id
    await fixture.publish(post_id, msk(10), message_id=1)
    failed_id, _ = await fixture.news(
        source_id, status=S.FAILED, created_at=msk(11), reason="bad_ai_response"
    )
    await fixture.ai_request(failed_id, created_at=msk(11), cost="0.004", ok=False)
    await fixture.ai_request(failed_id, created_at=msk(11), cost="0.003", ok=False)

    spending = (await fixture.report(session)).spending
    assert spending.cost == Decimal("0.009")
    assert (spending.requests, spending.failed) == (3, 2)
    assert spending.month_to_date == Decimal("0.009")
    assert news_id != failed_id


async def test_spending_split_by_kind(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Отбор и пересказ видны в отчёте отдельно."""
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    news_id, _ = await fixture.news(
        source_id, created_at=msk(9), published_at=msk(8), score=0.9, cost="0.002"
    )
    await fixture.ai_request(news_id, created_at=msk(9), cost="0.0004", kind="screen")
    screened_out, _ = await fixture.news(
        source_id, status=S.FILTERED, created_at=msk(10), reason="not_relevant"
    )
    await fixture.ai_request(screened_out, created_at=msk(10), cost="0.0004", kind="screen")

    spending = (await fixture.report(session)).spending
    assert {item.kind: (item.requests, item.cost) for item in spending.by_kind} == {
        "screen": (2, Decimal("0.0008")),
        "summary": (1, Decimal("0.002")),
    }
    assert spending.cost == Decimal("0.0028")


async def test_cached_tokens_share_in_spending(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    await fixture.news(
        source_id,
        created_at=msk(9),
        published_at=msk(8),
        score=0.9,
        tokens=(1000, 100),
        cached_tokens=750,
    )

    spending = (await fixture.report(session)).spending
    assert (spending.prompt_tokens, spending.cached_tokens) == (1000, 750)
    assert spending.cached_share == pytest.approx(0.75)
    assert spending.failed == 0


async def test_requests_of_deleted_news_remain(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Новость удалена — строка расхода остаётся с news_item_id = NULL."""
    fixture = Fixture(session_factory)
    await fixture.ai_request(None, created_at=msk(9), cost="0.007", ok=False)

    spending = (await fixture.report(session)).spending
    assert spending.cost == Decimal("0.007")
    assert (spending.requests, spending.failed) == (1, 1)


async def test_readers_block_counts_collected_stats(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Блок «Читатели» считает только посты с собранной статистикой."""
    fixture = Fixture(session_factory)
    first = await fixture.source("Bell")
    second = await fixture.source("Экономист")
    _, best = await fixture.news(first, created_at=msk(9), published_at=msk(8), score=0.9)
    _, good = await fixture.news(first, created_at=msk(9), published_at=msk(8), score=0.8)
    _, other = await fixture.news(second, created_at=msk(9), published_at=msk(8), score=0.7)
    _, fresh = await fixture.news(first, created_at=msk(9), published_at=msk(8), score=0.6)
    assert best and good and other and fresh
    await fixture.publish(best, msk(10), message_id=10, views=5200, forwards=40, reactions=12)
    await fixture.publish(good, msk(11), message_id=11, views=3800, forwards=10, reactions=5)
    await fixture.publish(other, msk(12), message_id=12, views=1000, forwards=1, reactions=0)
    await fixture.publish(fresh, msk(13), message_id=13)

    readers = (await fixture.report(session)).readers

    assert readers.posts == 3
    assert readers.views == 10000
    assert (readers.forwards, readers.reactions) == (51, 17)
    assert readers.views_per_post == 3333
    assert [post.views for post in readers.top] == [5200, 3800, 1000]
    assert readers.top[0].url is not None and readers.top[0].url.endswith("/10")
    assert readers.by_source == (("Bell", 4500), ("Экономист", 1000))


async def test_readers_block_empty_without_stats(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    _, post_id = await fixture.news(source_id, created_at=msk(9), published_at=msk(8), score=0.9)
    assert post_id
    await fixture.publish(post_id, msk(10), message_id=1)

    readers = (await fixture.report(session)).readers
    assert readers == type(readers)()
    assert readers.views_per_post is None


async def test_queue_state_and_errors(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    await fixture.news(
        source_id,
        status=S.APPROVED,
        created_at=msk(9),
        published_at=msk(9),
        score=0.6,
        title="Свежий",
    )
    await fixture.news(
        source_id,
        status=S.APPROVED,
        created_at=msk(9),
        published_at=msk(9) - timedelta(hours=20),
        score=0.8,
        title="Вчерашний",
    )
    await fixture.news(source_id, status=S.PENDING_REVIEW, created_at=msk(9), score=0.5)
    await fixture.news(source_id, status=S.FAILED, created_at=msk(9), reason="bad_ai_response")
    await fixture.incident(AlertKind.SOURCE_DOWN, msk(10), resolved=False)
    await fixture.incident(AlertKind.AI_DOWN, msk(11), resolved=True)
    await fixture.incident(AlertKind.PUBLISH_ERROR, msk(10, day=12), resolved=False)

    report = await fixture.report(session, paused=True)
    assert report.queue.in_queue == 2
    assert report.queue.pending_review == 1
    assert report.queue.best_title == "Свежий"  # приоритет учитывает свежесть
    assert report.queue.paused is True
    assert report.errors.open_incidents == 2
    assert report.errors.new_by_kind == {"source_down": 1, "ai_down": 1}
    assert report.errors.failed_news == 1


async def test_day_boundary_uses_moscow_midnight(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    _, late = await fixture.news(source_id, created_at=msk(23, 30), score=0.9)
    _, early = await fixture.news(source_id, created_at=msk(0, 10), score=0.8)
    _, yesterday = await fixture.news(source_id, created_at=msk(23, 30, day=13), score=0.7)
    assert late and early and yesterday
    await fixture.publish(late, msk(23, 40), message_id=1)
    await fixture.publish(yesterday, msk(23, 40, day=13), message_id=2)

    report = await fixture.report(session)
    assert report.funnel.collected == 2  # 23:30 и 00:10 — сегодня по Москве
    assert report.publications.total == 1
    assert report.publications.by_hour == {23: 1}
    assert report.previous.publications == 1
    assert report.previous.collected == 1


async def test_news_collected_yesterday_published_today(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    _, post_id = await fixture.news(
        source_id,
        created_at=msk(22, day=13),
        published_at=msk(21, day=13),
        score=0.9,
        post_created_at=msk(22, day=13),
    )
    assert post_id
    await fixture.publish(post_id, msk(9), message_id=1)

    report = await fixture.report(session)
    assert report.publications.total == 1, "в публикациях есть"
    assert report.funnel.collected == 0, "в воронке нет: собрана вчера"
    assert report.spending.cost == Decimal(0), "расход тоже вчерашний"
    assert report.previous.collected == 1


async def test_deleted_source_is_labelled(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    first = await fixture.source("A")
    second = await fixture.source("B")
    _, post_id = await fixture.news(second, created_at=msk(9), score=0.9)
    assert post_id
    await fixture.publish(post_id, msk(10), message_id=1)
    await fixture.news(first, created_at=msk(9), status=S.DUPLICATE, reason="duplicate:url:1")

    async with fixture.scope() as session_del:
        source = await session_del.get(Source, second)
        assert source is not None
        await session_del.delete(source)

    report = await fixture.report(session)
    assert [item.name for item in report.sources] == ["A"], "новости удалённого источника удалены"
    assert report.publications.total == 0


async def test_comparison_with_previous_day(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    _, today = await fixture.news(source_id, created_at=msk(9), score=0.9, cost="0.004")
    yesterday = msk(9, day=13)
    _, before = await fixture.news(
        source_id, created_at=yesterday, score=0.8, cost="0.001", post_created_at=yesterday
    )
    assert today and before
    await fixture.publish(today, msk(10), message_id=1)
    await fixture.publish(before, msk(10, day=13), message_id=2)

    report = await fixture.report(session)
    assert (report.publications.total, report.funnel.collected) == (1, 1)
    assert report.spending.cost == Decimal("0.004")
    assert report.previous.publications == 1
    assert report.previous.collected == 1
    assert report.previous.cost == Decimal("0.001")


async def test_queue_limited_counted_separately(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Вытесненные лимитом не смешиваются с фильтрами возраста, языка и слов."""
    fixture = Fixture(session_factory)
    source_id = await fixture.source("A")
    await fixture.news(source_id, status=S.FILTERED, created_at=msk(8), reason="queue_limit")
    await fixture.news(source_id, status=S.FILTERED, created_at=msk(8), reason="queue_limit")
    await fixture.news(source_id, status=S.FILTERED, created_at=msk(8), reason="too_old")
    await fixture.news(
        source_id, status=S.FILTERED, created_at=msk(8), reason="not_relevant", score=0.1
    )

    report = await fixture.report(session)
    funnel = report.funnel
    assert funnel.queue_limited == 2
    assert funnel.filtered == 1, "в «отсеяно фильтрами» только too_old"
    assert funnel.filtered_by_reason == {"too_old": 1}
    assert funnel.not_relevant == 1

    [stats] = report.sources
    assert stats.queue_limited == 2
    assert stats.filtered == 1
    assert stats.not_relevant == 1
