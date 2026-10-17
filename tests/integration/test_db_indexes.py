from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AiRequest, NewsItem, ProcessedPost, Publication, Source
from app.db.repositories.news import NewsRepository
from app.domain.enums import NewsStatus, PublicationStatus, SourceType
from app.pipeline.queue_limit import QUEUE_STATUSES

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CHANNEL = -1001234567890
INDEX_SCANS = ("Index Scan", "Index Only Scan", "Bitmap Index Scan")


@dataclass(frozen=True)
class Raw:
    source_id: int
    external_id: str
    url: str = "https://i.example/news/1"
    title: str = "Заголовок"
    text: str = "Текст новости"
    image_url: str | None = None
    published_at: datetime | None = NOW
    raw_meta: Mapping[str, Any] = field(default_factory=dict)


async def _seed(session: AsyncSession) -> None:
    """Немного данных: план не должен зависеть от объёма, но таблицы не пустые."""
    source = Source(type=SourceType.RSS, url="https://i.example/rss", name="I", interval_min=10)
    session.add(source)
    await session.flush()
    repo = NewsRepository(session)
    for index in range(3):
        item = await repo.add_if_new(Raw(source.id, str(index)))
        assert item is not None
        post = ProcessedPost(
            news_item_id=item.id,
            ai_title="t",
            ai_summary="s",
            hashtags=[],
            relevance_score=0.8,
            prompt_version="v4",
            prompt_tokens=10,
            completion_tokens=5,
            cost=Decimal("0.0001"),
        )
        session.add(post)
        session.add(
            AiRequest(
                news_item_id=item.id,
                kind="summary",
                model="m",
                prompt_tokens=10,
                cached_tokens=0,
                completion_tokens=5,
                cost=Decimal("0.0001"),
                ok=True,
            )
        )
        await session.flush()
        session.add(
            Publication(
                processed_post_id=post.id,
                channel_id=CHANNEL,
                status=PublicationStatus.SENT,
                message_id=index,
                published_at=NOW,
            )
        )
    await session.flush()


async def _plan(session: AsyncSession, statement: Any) -> str:
    """План запроса при выключенном полном переборе.

    Args:
        session: Сессия теста.
        statement: Запрос SQLAlchemy (select).

    Returns:
        Текст плана EXPLAIN одной строкой.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    compiled = statement.compile(session.bind, compile_kwargs={"literal_binds": True})
    rows = (await session.execute(text(f"EXPLAIN {compiled}"))).scalars()
    return "\n".join(rows)


def _uses_index(plan: str, index: str) -> bool:
    return index in plan and any(scan in plan for scan in INDEX_SCANS)


async def test_last_published_at_uses_channel_index(session: AsyncSession) -> None:
    await _seed(session)
    stmt = select(func.max(Publication.published_at)).where(
        Publication.channel_id == CHANNEL, Publication.status == PublicationStatus.SENT
    )
    plan = await _plan(session, stmt)
    assert _uses_index(plan, "ix_publications_channel_published"), plan
    assert "Seq Scan on publications" not in plan, plan


async def test_cost_since_uses_ai_requests_index(session: AsyncSession) -> None:
    await _seed(session)
    stmt = select(func.coalesce(func.sum(AiRequest.cost), 0)).where(
        AiRequest.created_at >= NOW - timedelta(days=1)
    )
    plan = await _plan(session, stmt)
    assert _uses_index(plan, "ix_ai_requests_created_at"), plan
    assert "Seq Scan on ai_requests" not in plan, plan


async def test_queue_counts_by_source_uses_source_status_index(session: AsyncSession) -> None:
    await _seed(session)
    statuses: Sequence[NewsStatus] = QUEUE_STATUSES
    stmt = (
        select(NewsItem.source_id, func.count())
        .join(ProcessedPost, ProcessedPost.news_item_id == NewsItem.id)
        .where(NewsItem.status.in_(statuses))
        .group_by(NewsItem.source_id)
    )
    plan = await _plan(session, stmt)
    assert "Seq Scan on news_items" not in plan, plan
    assert any(scan in plan for scan in INDEX_SCANS), plan


async def test_queued_by_source_uses_source_status_index(session: AsyncSession) -> None:
    await _seed(session)
    stmt = (
        select(NewsItem.id, NewsItem.status)
        .join(ProcessedPost, ProcessedPost.news_item_id == NewsItem.id)
        .where(NewsItem.source_id == 1, NewsItem.status.in_(QUEUE_STATUSES))
    )
    plan = await _plan(session, stmt)
    assert _uses_index(plan, "ix_news_items_source_status"), plan


async def test_funnel_by_created_at_uses_index(session: AsyncSession) -> None:
    await _seed(session)
    news_plan = await _plan(
        session,
        select(func.count())
        .select_from(NewsItem)
        .where(NewsItem.created_at >= NOW - timedelta(days=1), NewsItem.created_at < NOW),
    )
    assert _uses_index(news_plan, "ix_news_items_created_at"), news_plan

    posts_plan = await _plan(
        session,
        select(func.count())
        .select_from(ProcessedPost)
        .where(ProcessedPost.created_at >= NOW - timedelta(days=1), ProcessedPost.created_at < NOW),
    )
    assert _uses_index(posts_plan, "ix_processed_posts_created_at"), posts_plan
