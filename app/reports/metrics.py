"""Подсчёт метрик тестового прогона и сравнение с целями MVP."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MetricsConfig
from app.db.models import NewsItem, ProcessedPost, Publication, Source
from app.domain.enums import NewsStatus, PublicationStatus

P95 = 0.95
SECONDS_PER_MINUTE = 60
NO_DATA = "нет данных"
DUPLICATE_PREFIX = "duplicate:"
REVIEWED_STATUSES = (
    NewsStatus.PENDING_REVIEW,
    NewsStatus.APPROVED,
    NewsStatus.SCHEDULED,
    NewsStatus.PUBLISHED,
    NewsStatus.REJECTED,
)


@dataclass(frozen=True)
class Period:
    """Полуинтервал [start, end) в UTC-aware времени."""

    start: datetime
    end: datetime

    @classmethod
    def from_dates(cls, first: date, last: date, tz: ZoneInfo) -> Period:
        """Период с начала дня first до конца дня last в часовом поясе tz."""
        return cls(
            start=datetime.combine(first, time.min, tzinfo=tz),
            end=datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz),
        )

    @classmethod
    def day(cls, day: date, tz: ZoneInfo) -> Period:
        """Сутки [00:00, 24:00) в часовом поясе tz.

        При переходе на летнее или зимнее время сутки длятся 23 или 25 часов.
        """
        return cls.from_dates(day, day, tz)


@dataclass
class RunReport:
    """Метрики за период."""

    publications: int = 0
    delay_median_min: float | None = None
    delay_p95_min: float | None = None
    rejected_share: float | None = None
    duplicates_by_level: dict[str, int] = field(default_factory=dict)
    duplicate_share: float | None = None
    cost_by_day: dict[date, Decimal] = field(default_factory=dict)
    source_errors: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricCheck:
    """Сравнение метрики с целью: ok=None, если данных нет."""

    name: str
    value: float | None
    target: float
    ok: bool | None


def percentile(values: Sequence[float], q: float) -> float:
    """Перцентиль q (0..1) с линейной интерполяцией."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


async def collect_report(session: AsyncSession, period: Period, tz: ZoneInfo) -> RunReport:
    """Считает метрики тестового прогона за период.

    Args:
        session: Сессия БД.
        period: Период отчёта.
        tz: Часовой пояс для расхода по дням.

    Returns:
        Метрики; для метрик без данных значение None.
    """
    report = RunReport()
    in_period_pub = (Publication.published_at >= period.start) & (
        Publication.published_at < period.end
    )
    sent = Publication.status == PublicationStatus.SENT

    rows = (
        await session.execute(
            select(
                Publication.published_at, NewsItem.published_at, NewsItem.cluster_id, NewsItem.id
            )
            .join(ProcessedPost, ProcessedPost.id == Publication.processed_post_id)
            .join(NewsItem, NewsItem.id == ProcessedPost.news_item_id)
            .where(sent, in_period_pub)
            .order_by(Publication.published_at)
        )
    ).all()
    report.publications = len(rows)
    delays = [
        (published - source_time).total_seconds() / SECONDS_PER_MINUTE
        for published, source_time, _, _ in rows
        if published is not None and source_time is not None
    ]
    if delays:
        report.delay_median_min = statistics.median(delays)
        report.delay_p95_min = percentile(delays, P95)

    if rows:
        seen_clusters: set[int] = set()
        repeated = 0
        for _, _, cluster_id, news_id in rows:
            cluster = cluster_id if cluster_id is not None else news_id
            if cluster in seen_clusters:
                repeated += 1
            seen_clusters.add(cluster)
        report.duplicate_share = repeated / len(rows)

    in_period_post = (ProcessedPost.created_at >= period.start) & (
        ProcessedPost.created_at < period.end
    )
    status_counts = dict(
        (
            await session.execute(
                select(NewsItem.status, func.count())
                .join(ProcessedPost, ProcessedPost.news_item_id == NewsItem.id)
                .where(in_period_post, NewsItem.status.in_(REVIEWED_STATUSES))
                .group_by(NewsItem.status)
            )
        ).all()
    )
    reviewed = sum(status_counts.values())
    if reviewed:
        report.rejected_share = status_counts.get(NewsStatus.REJECTED, 0) / reviewed

    reasons = (
        await session.execute(
            select(NewsItem.reject_reason).where(
                NewsItem.status == NewsStatus.DUPLICATE,
                NewsItem.created_at >= period.start,
                NewsItem.created_at < period.end,
            )
        )
    ).scalars()
    for reason in reasons:
        parts = (reason or "").removeprefix(DUPLICATE_PREFIX).split(":")
        level = parts[0] if reason and reason.startswith(DUPLICATE_PREFIX) else "unknown"
        report.duplicates_by_level[level] = report.duplicates_by_level.get(level, 0) + 1

    for created_at, cost in (
        await session.execute(
            select(ProcessedPost.created_at, ProcessedPost.cost).where(in_period_post)
        )
    ).all():
        day = created_at.astimezone(tz).date()
        report.cost_by_day[day] = report.cost_by_day.get(day, Decimal(0)) + cost

    for name, errors in (
        await session.execute(
            select(Source.name, Source.error_count)
            .where(Source.error_count > 0)
            .order_by(Source.name)
        )
    ).all():
        report.source_errors[name] = errors
    return report


def compare_with_targets(report: RunReport, cfg: MetricsConfig) -> list[MetricCheck]:
    """Сравнивает метрики с целями MVP из секции metrics."""

    def check(name: str, value: float | None, target: float) -> MetricCheck:
        return MetricCheck(name, value, target, None if value is None else value <= target)

    return [
        check("медиана задержки, мин", report.delay_median_min, cfg.max_delay_min),
        check("доля отклонённых модератором", report.rejected_share, cfg.max_rejected_share),
        check("доля дублей в канале", report.duplicate_share, cfg.max_duplicate_share),
    ]
