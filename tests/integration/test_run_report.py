from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MetricsConfig
from app.db.models import NewsItem, ProcessedPost, Publication, Source
from app.domain.enums import NewsStatus, PublicationStatus, SourceType
from scripts.run_report import (
    MetricCheck,
    Period,
    RunReport,
    collect_report,
    compare_with_targets,
    format_report,
    percentile,
)
from tests.config_helpers import base_section

pytestmark = pytest.mark.integration

MSK = ZoneInfo("Europe/Moscow")
DAY = date(2026, 10, 10)
PERIOD = Period.from_dates(DAY, DAY + timedelta(days=1), MSK)
BASE = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
CFG = MetricsConfig(**base_section("metrics"))


async def _source(session: AsyncSession, name: str, errors: int = 0) -> Source:
    source = Source(
        type=SourceType.RSS,
        url=f"https://{name}.example/rss",
        name=name,
        interval_min=10,
        error_count=errors,
    )
    session.add(source)
    await session.flush()
    return source


async def _news(
    session: AsyncSession,
    source: Source,
    ext: str,
    status: NewsStatus,
    published_at: datetime | None = BASE,
    cluster_id: int | None = None,
    reject_reason: str | None = None,
    created_at: datetime = BASE,
) -> NewsItem:
    item = NewsItem(
        source_id=source.id,
        external_id=ext,
        url=f"https://x.example/{ext}",
        title=ext,
        raw_text="t",
        status=status,
        published_at=published_at,
        cluster_id=cluster_id,
        reject_reason=reject_reason,
        created_at=created_at,
    )
    session.add(item)
    await session.flush()
    return item


async def _post(
    session: AsyncSession, news: NewsItem, cost: str, created_at: datetime = BASE
) -> ProcessedPost:
    post = ProcessedPost(
        news_item_id=news.id,
        ai_title="t",
        ai_summary="s",
        hashtags=[],
        relevance_score=0.9,
        prompt_version="v1",
        prompt_tokens=1,
        completion_tokens=1,
        cost=Decimal(cost),
        created_at=created_at,
    )
    session.add(post)
    await session.flush()
    return post


async def _publish(session: AsyncSession, post: ProcessedPost, at: datetime) -> None:
    session.add(
        Publication(
            processed_post_id=post.id,
            channel_id=-100,
            status=PublicationStatus.SENT,
            message_id=1,
            published_at=at,
        )
    )
    await session.flush()


async def test_all_metrics(session: AsyncSession) -> None:
    src = await _source(session, "alpha")
    await _source(session, "broken", errors=4)

    delays = [5, 10, 20, 30]
    published: list[NewsItem] = []
    for i, minutes in enumerate(delays):
        news = await _news(session, src, f"p{i}", NewsStatus.PUBLISHED)
        post = await _post(session, news, "0.010000")
        await _publish(session, post, BASE + timedelta(minutes=minutes))
        published.append(news)
    # Пост из того же кластера, что и p0, — дубль, дошедший до канала.
    dup_news = await _news(session, src, "p-dup", NewsStatus.PUBLISHED, cluster_id=published[0].id)
    dup_post = await _post(session, dup_news, "0.010000", created_at=BASE + timedelta(days=1))
    await _publish(session, dup_post, BASE + timedelta(minutes=40))
    no_date = await _news(session, src, "p-nodate", NewsStatus.PUBLISHED, published_at=None)
    await _publish(session, await _post(session, no_date, "0.010000"), BASE + timedelta(minutes=50))

    rejected = await _news(session, src, "r1", NewsStatus.REJECTED)
    await _post(session, rejected, "0.005000")
    await _news(session, src, "d1", NewsStatus.DUPLICATE, reject_reason="duplicate:url:1")
    await _news(session, src, "d2", NewsStatus.DUPLICATE, reject_reason="duplicate:hash:1")
    await _news(session, src, "d3", NewsStatus.DUPLICATE, reject_reason="duplicate:hash:2")
    outside = await _news(
        session, src, "old", NewsStatus.PUBLISHED, created_at=BASE - timedelta(days=5)
    )
    old_post = await _post(session, outside, "9.000000", created_at=BASE - timedelta(days=5))
    await _publish(session, old_post, BASE - timedelta(days=5))

    report = await collect_report(session, PERIOD, MSK)

    assert report.publications == 6
    assert report.delay_median_min == 20
    assert report.delay_p95_min == pytest.approx(percentile([5, 10, 20, 30, 40], 0.95))
    assert report.duplicate_share == pytest.approx(1 / 6)
    assert report.rejected_share == pytest.approx(1 / 7)
    assert report.duplicates_by_level == {"url": 1, "hash": 2}
    assert report.cost_by_day == {
        DAY: Decimal("0.055000"),
        DAY + timedelta(days=1): Decimal("0.010000"),
    }
    assert report.source_errors == {"broken": 4}


async def test_empty_period(session: AsyncSession) -> None:
    report = await collect_report(session, PERIOD, MSK)
    assert report == RunReport()
    checks = compare_with_targets(report, CFG)
    assert all(check.ok is None for check in checks)
    text = format_report(report, checks)
    assert "Публикаций: 0" in text
    assert "нет данных" in text
    assert "| нет данных" in text


def test_compare_with_targets() -> None:
    report = RunReport(delay_median_min=12.0, rejected_share=0.3, duplicate_share=0.0)
    checks = compare_with_targets(report, CFG)
    assert [(c.name, c.ok) for c in checks] == [
        ("медиана задержки, мин", True),
        ("доля отклонённых модератором", False),
        ("доля дублей в канале", True),
    ]
    text = format_report(report, checks)
    assert "медиана задержки, мин | 12.00 | ≤ 15 | ok" in text
    assert "доля отклонённых модератором | 0.30 | ≤ 0.2 | fail" in text


def test_period_in_channel_timezone() -> None:
    assert PERIOD.start == datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
    assert PERIOD.end == datetime(2026, 10, 11, 21, 0, tzinfo=UTC)
    assert MetricCheck("x", None, 1, None).ok is None
