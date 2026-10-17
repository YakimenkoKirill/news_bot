"""Данные ежедневного отчёта: все цифры дня одним вызовом collect_daily."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AiRequest, Incident, NewsItem, ProcessedPost, Publication, Source
from app.domain.enums import NewsStatus, PublicationStatus
from app.pipeline.ai.processor import REASON_NOT_RELEVANT
from app.pipeline.queue_limit import REASON_QUEUE_LIMIT
from app.reports.metrics import DUPLICATE_PREFIX, Period, RunReport, collect_report

CHANNEL_PREFIX = "-100"
POST_URL = "https://t.me/c/{chat}/{message_id}"
DELETED_SOURCE = "удалённый источник #{source_id}"
REASON_BAD_AI = "bad_ai_response"
HOURS_IN_DAY = 24
PUBLISH_QUEUE_STATUSES = (NewsStatus.APPROVED, NewsStatus.SCHEDULED)


@dataclass(frozen=True)
class DailyContext:
    """Настройки, нужные для подсчёта отчёта.

    Attributes:
        tz: Часовой пояс канала (app.timezone).
        daily_budget: Дневной бюджет на модель в долларах (ai.daily_budget).
        decay_per_h: Снижение приоритета за час (publisher.score_decay_per_h).
        top_posts: Сколько лучших постов дня показывать (daily_report.top_posts).
        channel_id: Канал публикации (для ссылок на посты).
    """

    tz: ZoneInfo
    daily_budget: Decimal
    decay_per_h: float
    top_posts: int
    channel_id: int


@dataclass(frozen=True)
class TopPost:
    """Пост дня для списка лучших."""

    title: str
    source: str
    score: float
    message_id: int | None
    url: str | None


@dataclass(frozen=True)
class Publications:
    """Публикации за период.

    Неудачные попытки (failed) считаются по времени создания пересказа: у них
    нет времени публикации.
    """

    total: int = 0
    scheduled: int = 0
    manual: int = 0
    edited: int = 0
    failed: int = 0
    by_hour: dict[int, int] = field(default_factory=dict)
    top: tuple[TopPost, ...] = ()


@dataclass(frozen=True)
class SourceStats:
    """Итоги одного источника за период."""

    source_id: int
    name: str
    enabled: bool
    error_count: int
    collected: int = 0
    published: int = 0
    duplicates: int = 0
    filtered: int = 0
    not_relevant: int = 0
    rejected: int = 0
    ai_failed: int = 0
    queue_limited: int = 0
    avg_score: float | None = None
    cost: Decimal = Decimal(0)


@dataclass(frozen=True)
class Funnel:
    """Путь новостей, собранных за период, по текущему статусу.

    Новость, собранная вчера и опубликованная сегодня, в воронку не попадает
    (она считается по дате сбора), но входит в блок «Публикации».
    """

    collected: int = 0
    duplicates: int = 0
    duplicates_by_level: dict[str, int] = field(default_factory=dict)
    filtered: int = 0
    filtered_by_reason: dict[str, int] = field(default_factory=dict)
    processed: int = 0
    not_relevant: int = 0
    ai_failed: int = 0
    queue_limited: int = 0
    pending_review: int = 0
    rejected: int = 0
    in_queue: int = 0
    published: int = 0


@dataclass(frozen=True)
class Spending:
    """Расход на модель за период.

    Считается по ai_requests, то есть по каждому оплаченному запросу, включая
    неудачные попытки, из-за которых пересказа так и не появилось.

    Attributes:
        cost: Расход за период.
        requests: Сколько запросов ушло в модель.
        failed: Из них неудачных (ответ не разобран).
        prompt_tokens: Входные токены, включая кэшированные.
        cached_tokens: Входные токены, оплаченные по цене кэша.
        completion_tokens: Выходные токены.
        budget_share: Доля дневного бюджета.
        cost_per_post: Расход на один вышедший пост.
        month_to_date: Расход с начала месяца.
    """

    cost: Decimal = Decimal(0)
    requests: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    budget_share: float | None = None
    cost_per_post: Decimal | None = None
    month_to_date: Decimal = Decimal(0)

    @property
    def cached_share(self) -> float | None:
        """Доля входных токенов, пришедших из кэша; None — входных токенов не было."""
        if not self.prompt_tokens:
            return None
        return self.cached_tokens / self.prompt_tokens


@dataclass(frozen=True)
class Quality:
    """Качество публикаций за период."""

    delay_median_min: float | None = None
    delay_p95_min: float | None = None
    duplicate_share: float | None = None
    avg_published_score: float | None = None
    rejected_share: float | None = None


@dataclass(frozen=True)
class QueueState:
    """Состояние очереди на момент отчёта."""

    in_queue: int = 0
    pending_review: int = 0
    best_title: str | None = None
    best_score: float | None = None
    paused: bool = False


@dataclass(frozen=True)
class Errors:
    """Ошибки за период."""

    open_incidents: int = 0
    new_by_kind: dict[str, int] = field(default_factory=dict)
    failed_news: int = 0


@dataclass(frozen=True)
class Headline:
    """Главные цифры периода для сравнения с предыдущим."""

    publications: int = 0
    collected: int = 0
    cost: Decimal = Decimal(0)


@dataclass(frozen=True)
class DailyReport:
    """Полный ежедневный отчёт."""

    period: Period
    publications: Publications
    sources: tuple[SourceStats, ...]
    funnel: Funnel
    spending: Spending
    quality: Quality
    queue: QueueState
    errors: Errors
    previous: Headline


def post_url(channel_id: int, message_id: int | None) -> str | None:
    """Ссылка на пост канала или None, если сообщения нет."""
    if message_id is None:
        return None
    chat = str(channel_id).removeprefix(CHANNEL_PREFIX)
    return POST_URL.format(chat=chat, message_id=message_id)


def _published_news_ids(period: Period) -> Select[int]:
    """Id новостей, опубликованных за период."""
    return (
        select(ProcessedPost.news_item_id)
        .join(Publication, Publication.processed_post_id == ProcessedPost.id)
        .where(
            Publication.status == PublicationStatus.SENT,
            Publication.published_at >= period.start,
            Publication.published_at < period.end,
        )
    )


async def _publications(session: AsyncSession, period: Period, ctx: DailyContext) -> Publications:
    sent = (
        Publication.status == PublicationStatus.SENT,
        Publication.published_at >= period.start,
        Publication.published_at < period.end,
    )
    totals = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Publication.initiated_by.is_(None)),
                func.count().filter(Publication.initiated_by.is_not(None)),
                func.count().filter(ProcessedPost.edited_by.is_not(None)),
            )
            .select_from(Publication)
            .join(ProcessedPost, ProcessedPost.id == Publication.processed_post_id)
            .where(*sent)
        )
    ).one()
    failed = (
        await session.execute(
            select(func.count())
            .select_from(Publication)
            .join(ProcessedPost, ProcessedPost.id == Publication.processed_post_id)
            .where(
                Publication.status == PublicationStatus.FAILED,
                ProcessedPost.created_at >= period.start,
                ProcessedPost.created_at < period.end,
            )
        )
    ).scalar_one()
    hour = func.extract("hour", func.timezone(str(ctx.tz), Publication.published_at))
    by_hour = {
        int(value): count
        for value, count in (
            await session.execute(
                select(hour, func.count()).select_from(Publication).where(*sent).group_by(hour)
            )
        ).all()
    }
    top_rows = (
        await session.execute(
            select(
                ProcessedPost.ai_title,
                ProcessedPost.relevance_score,
                Publication.message_id,
                Source.name,
                NewsItem.source_id,
            )
            .select_from(Publication)
            .join(ProcessedPost, ProcessedPost.id == Publication.processed_post_id)
            .join(NewsItem, NewsItem.id == ProcessedPost.news_item_id)
            .outerjoin(Source, Source.id == NewsItem.source_id)
            .where(*sent)
            .order_by(ProcessedPost.relevance_score.desc(), Publication.published_at)
            .limit(ctx.top_posts)
        )
    ).all()
    top = tuple(
        TopPost(
            title=title,
            source=name if name else DELETED_SOURCE.format(source_id=source_id),
            score=score,
            message_id=message_id,
            url=post_url(ctx.channel_id, message_id),
        )
        for title, score, message_id, name, source_id in top_rows
    )
    return Publications(
        total=totals[0],
        scheduled=totals[1],
        manual=totals[2],
        edited=totals[3],
        failed=failed,
        by_hour=by_hour,
        top=top,
    )


async def _sources(session: AsyncSession, period: Period) -> tuple[SourceStats, ...]:
    in_period = (NewsItem.created_at >= period.start, NewsItem.created_at < period.end)
    reason = func.split_part(func.coalesce(NewsItem.reject_reason, ""), ":", 1)
    rows = (
        await session.execute(
            select(
                NewsItem.source_id,
                func.count(),
                func.count().filter(NewsItem.status == NewsStatus.DUPLICATE),
                func.count().filter(
                    NewsItem.status == NewsStatus.FILTERED,
                    reason.notin_((REASON_NOT_RELEVANT, REASON_QUEUE_LIMIT)),
                ),
                func.count().filter(
                    NewsItem.status == NewsStatus.FILTERED, reason == REASON_NOT_RELEVANT
                ),
                func.count().filter(
                    NewsItem.status == NewsStatus.FILTERED, reason == REASON_QUEUE_LIMIT
                ),
                func.count().filter(NewsItem.status == NewsStatus.REJECTED),
                func.count().filter(NewsItem.status == NewsStatus.FAILED, reason == REASON_BAD_AI),
            )
            .where(*in_period)
            .group_by(NewsItem.source_id)
        )
    ).all()
    collected = {row[0]: row for row in rows}

    published_rows = (
        await session.execute(
            select(NewsItem.source_id, func.count())
            .where(NewsItem.id.in_(_published_news_ids(period)))
            .group_by(NewsItem.source_id)
        )
    ).all()
    published = dict(published_rows)

    money_rows = (
        await session.execute(
            select(
                NewsItem.source_id,
                func.avg(ProcessedPost.relevance_score),
                func.coalesce(func.sum(ProcessedPost.cost), 0),
            )
            .join(NewsItem, NewsItem.id == ProcessedPost.news_item_id)
            .where(ProcessedPost.created_at >= period.start, ProcessedPost.created_at < period.end)
            .group_by(NewsItem.source_id)
        )
    ).all()
    money = {row[0]: (row[1], row[2]) for row in money_rows}

    source_ids = set(collected) | set(published) | set(money)
    if not source_ids:
        return ()
    known = {
        row.id: row
        for row in (
            await session.execute(select(Source).where(Source.id.in_(source_ids)))
        ).scalars()
    }

    stats: list[SourceStats] = []
    for source_id in source_ids:
        row = collected.get(source_id)
        score, cost = money.get(source_id, (None, Decimal(0)))
        source = known.get(source_id)
        stats.append(
            SourceStats(
                source_id=source_id,
                name=source.name if source else DELETED_SOURCE.format(source_id=source_id),
                enabled=bool(source.enabled) if source else False,
                error_count=source.error_count if source else 0,
                collected=row[1] if row else 0,
                published=published.get(source_id, 0),
                duplicates=row[2] if row else 0,
                filtered=row[3] if row else 0,
                not_relevant=row[4] if row else 0,
                queue_limited=row[5] if row else 0,
                rejected=row[6] if row else 0,
                ai_failed=row[7] if row else 0,
                avg_score=float(score) if score is not None else None,
                cost=Decimal(cost),
            )
        )
    stats.sort(key=lambda item: (item.published, item.collected), reverse=True)
    return tuple(stats)


async def _funnel(session: AsyncSession, period: Period) -> Funnel:
    in_period = (NewsItem.created_at >= period.start, NewsItem.created_at < period.end)
    counts = dict(
        (
            await session.execute(
                select(NewsItem.status, func.count()).where(*in_period).group_by(NewsItem.status)
            )
        ).all()
    )
    reason_rows = (
        await session.execute(
            select(NewsItem.status, NewsItem.reject_reason, func.count())
            .where(
                *in_period,
                NewsItem.status.in_((NewsStatus.DUPLICATE, NewsStatus.FILTERED)),
            )
            .group_by(NewsItem.status, NewsItem.reject_reason)
        )
    ).all()
    duplicates_by_level: dict[str, int] = {}
    filtered_by_reason: dict[str, int] = {}
    not_relevant = 0
    queue_limited = 0
    for status, reason, count in reason_rows:
        text = reason or ""
        if status is NewsStatus.DUPLICATE:
            level = text.removeprefix(DUPLICATE_PREFIX).split(":")[0] if text else "unknown"
            duplicates_by_level[level] = duplicates_by_level.get(level, 0) + count
            continue
        key = text.split(":")[0] if text else "unknown"
        if key == REASON_NOT_RELEVANT:
            not_relevant += count
            continue
        if key == REASON_QUEUE_LIMIT:
            queue_limited += count
            continue
        filtered_by_reason[key] = filtered_by_reason.get(key, 0) + count

    ai_failed = (
        await session.execute(
            select(func.count())
            .select_from(NewsItem)
            .where(
                *in_period,
                NewsItem.status == NewsStatus.FAILED,
                func.split_part(func.coalesce(NewsItem.reject_reason, ""), ":", 1) == REASON_BAD_AI,
            )
        )
    ).scalar_one()
    processed = (
        await session.execute(
            select(func.count())
            .select_from(ProcessedPost)
            .join(NewsItem, NewsItem.id == ProcessedPost.news_item_id)
            .where(*in_period)
        )
    ).scalar_one()
    in_queue = sum(counts.get(status, 0) for status in PUBLISH_QUEUE_STATUSES)
    return Funnel(
        collected=sum(counts.values()),
        duplicates=counts.get(NewsStatus.DUPLICATE, 0),
        duplicates_by_level=duplicates_by_level,
        filtered=sum(filtered_by_reason.values()),
        filtered_by_reason=filtered_by_reason,
        processed=processed,
        not_relevant=not_relevant,
        ai_failed=ai_failed,
        queue_limited=queue_limited,
        pending_review=counts.get(NewsStatus.PENDING_REVIEW, 0),
        rejected=counts.get(NewsStatus.REJECTED, 0),
        in_queue=in_queue,
        published=counts.get(NewsStatus.PUBLISHED, 0),
    )


async def _spending(
    session: AsyncSession, period: Period, ctx: DailyContext, publications: int
) -> Spending:
    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(AiRequest.cost), 0),
                func.count(),
                func.coalesce(func.sum(AiRequest.prompt_tokens), 0),
                func.coalesce(func.sum(AiRequest.cached_tokens), 0),
                func.coalesce(func.sum(AiRequest.completion_tokens), 0),
                func.count().filter(AiRequest.ok.is_(False)),
            ).where(AiRequest.created_at >= period.start, AiRequest.created_at < period.end)
        )
    ).one()
    cost = Decimal(totals[0])
    month_start = period.start.astimezone(ctx.tz).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    month = Decimal(
        (
            await session.execute(
                select(func.coalesce(func.sum(AiRequest.cost), 0)).where(
                    AiRequest.created_at >= month_start,
                    AiRequest.created_at < period.end,
                )
            )
        ).scalar_one()
    )
    budget_share = float(cost / ctx.daily_budget) if ctx.daily_budget else None
    return Spending(
        cost=cost,
        requests=totals[1],
        failed=totals[5],
        prompt_tokens=totals[2],
        cached_tokens=totals[3],
        completion_tokens=totals[4],
        budget_share=budget_share,
        cost_per_post=cost / publications if publications else None,
        month_to_date=month,
    )


async def _quality(session: AsyncSession, period: Period, ctx: DailyContext) -> Quality:
    base: RunReport = await collect_report(session, period, ctx.tz)
    avg_score = (
        await session.execute(
            select(func.avg(ProcessedPost.relevance_score))
            .select_from(Publication)
            .join(ProcessedPost, ProcessedPost.id == Publication.processed_post_id)
            .where(
                Publication.status == PublicationStatus.SENT,
                Publication.published_at >= period.start,
                Publication.published_at < period.end,
            )
        )
    ).scalar_one()
    return Quality(
        delay_median_min=base.delay_median_min,
        delay_p95_min=base.delay_p95_min,
        duplicate_share=base.duplicate_share,
        avg_published_score=float(avg_score) if avg_score is not None else None,
        rejected_share=base.rejected_share,
    )


async def _queue(session: AsyncSession, ctx: DailyContext, paused: bool) -> QueueState:
    counts = dict(
        (
            await session.execute(
                select(NewsItem.status, func.count())
                .where(
                    NewsItem.status.in_(
                        (*PUBLISH_QUEUE_STATUSES, NewsStatus.PENDING_REVIEW),
                    )
                )
                .group_by(NewsItem.status)
            )
        ).all()
    )
    published = (
        func.extract("epoch", func.coalesce(NewsItem.published_at, NewsItem.created_at)) / 3600
    )
    best = (
        await session.execute(
            select(ProcessedPost.ai_title, ProcessedPost.relevance_score)
            .join(NewsItem, NewsItem.id == ProcessedPost.news_item_id)
            .where(NewsItem.status.in_(PUBLISH_QUEUE_STATUSES))
            .order_by(
                (ProcessedPost.relevance_score + ctx.decay_per_h * published).desc(),
                NewsItem.id,
            )
            .limit(1)
        )
    ).first()
    return QueueState(
        in_queue=sum(counts.get(status, 0) for status in PUBLISH_QUEUE_STATUSES),
        pending_review=counts.get(NewsStatus.PENDING_REVIEW, 0),
        best_title=best[0] if best else None,
        best_score=best[1] if best else None,
        paused=paused,
    )


async def _errors(session: AsyncSession, period: Period) -> Errors:
    open_incidents = (
        await session.execute(
            select(func.count()).select_from(Incident).where(Incident.resolved_at.is_(None))
        )
    ).scalar_one()
    new_by_kind = dict(
        (
            await session.execute(
                select(Incident.kind, func.count())
                .where(Incident.first_seen_at >= period.start, Incident.first_seen_at < period.end)
                .group_by(Incident.kind)
            )
        ).all()
    )
    failed_news = (
        await session.execute(
            select(func.count())
            .select_from(NewsItem)
            .where(
                NewsItem.created_at >= period.start,
                NewsItem.created_at < period.end,
                NewsItem.status == NewsStatus.FAILED,
            )
        )
    ).scalar_one()
    return Errors(open_incidents=open_incidents, new_by_kind=new_by_kind, failed_news=failed_news)


async def _headline(session: AsyncSession, period: Period) -> Headline:
    """Главные цифры периода: публикации, собранные новости и расход."""
    publications = (
        await session.execute(
            select(func.count())
            .select_from(Publication)
            .where(
                Publication.status == PublicationStatus.SENT,
                Publication.published_at >= period.start,
                Publication.published_at < period.end,
            )
        )
    ).scalar_one()
    collected = (
        await session.execute(
            select(func.count())
            .select_from(NewsItem)
            .where(NewsItem.created_at >= period.start, NewsItem.created_at < period.end)
        )
    ).scalar_one()
    cost = (
        await session.execute(
            select(func.coalesce(func.sum(AiRequest.cost), 0)).where(
                AiRequest.created_at >= period.start, AiRequest.created_at < period.end
            )
        )
    ).scalar_one()
    return Headline(publications=publications, collected=collected, cost=Decimal(cost))


async def collect_daily(
    session: AsyncSession,
    period: Period,
    previous: Period,
    ctx: DailyContext,
    *,
    paused: bool = False,
) -> DailyReport:
    """Собирает все цифры ежедневного отчёта.

    Каждая группа считается отдельным агрегирующим запросом, поэтому отчёт за
    длинный период не тянет строки в память. Публикации считаются по времени
    выхода в канал, а воронка — по времени сбора новости, поэтому новость,
    собранная вчера и опубликованная сегодня, попадает в «Публикации», но не в
    воронку сегодняшнего дня.

    Args:
        session: Сессия БД.
        period: Период отчёта.
        previous: Предыдущий период для сравнения.
        ctx: Настройки отчёта.
        paused: Стоит ли пайплайн на паузе сейчас.

    Returns:
        Отчёт со всеми группами метрик.
    """
    publications = await _publications(session, period, ctx)
    return DailyReport(
        period=period,
        publications=publications,
        sources=await _sources(session, period),
        funnel=await _funnel(session, period),
        spending=await _spending(session, period, ctx, publications.total),
        quality=await _quality(session, period, ctx),
        queue=await _queue(session, ctx, paused),
        errors=await _errors(session, period),
        previous=await _headline(session, previous),
    )
