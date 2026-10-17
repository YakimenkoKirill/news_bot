from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NewsItem, ProcessedPost, Publication, Source
from app.db.repositories.posts import PendingStats, PublicationRepository
from app.domain.enums import NewsStatus, PublicationStatus, SourceType

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CHANNEL = -1001234567890
OTHER_CHANNEL = -1009999999999
DELAY = timedelta(hours=24)
MAX_AGE = timedelta(hours=72)
SINCE = NOW - MAX_AGE
UNTIL = NOW - DELAY


async def _source(session: AsyncSession) -> int:
    existing = await session.scalar(select(Source.id).limit(1))
    if existing is not None:
        return int(existing)
    source = Source(type=SourceType.RSS, url="https://p.example/rss", name="P", interval_min=10)
    session.add(source)
    await session.flush()
    return source.id


_counter = {"value": 0}


async def _publication(
    session: AsyncSession,
    *,
    published_at: datetime | None,
    status: PublicationStatus = PublicationStatus.SENT,
    channel_id: int = CHANNEL,
    message_id: int | None = None,
    stats_at: datetime | None = None,
) -> int:
    source_id = await _source(session)
    _counter["value"] += 1
    index = _counter["value"]
    news = NewsItem(
        source_id=source_id,
        external_id=f"p{index}",
        url=f"https://p.example/{index}",
        title=f"Новость {index}",
        raw_text="текст",
        status=NewsStatus.PUBLISHED,
    )
    session.add(news)
    await session.flush()
    post = ProcessedPost(
        news_item_id=news.id,
        ai_title=f"Заголовок {index}",
        ai_summary="Пересказ",
        hashtags=[],
        relevance_score=0.8,
        prompt_version="v4",
        prompt_tokens=1,
        completion_tokens=1,
        cost=Decimal("0.000001"),
    )
    session.add(post)
    await session.flush()
    publication = Publication(
        processed_post_id=post.id,
        channel_id=channel_id,
        status=status,
        message_id=message_id if message_id is not None else 1000 + index,
        published_at=published_at,
        stats_at=stats_at,
    )
    session.add(publication)
    await session.flush()
    return publication.id


async def _row(session: AsyncSession, publication_id: int) -> Publication:
    publication = await session.get(Publication, publication_id)
    assert publication is not None
    await session.refresh(publication)
    return publication


async def test_list_for_stats_window(session: AsyncSession) -> None:
    ready = await _publication(session, published_at=NOW - timedelta(hours=30))
    too_fresh = await _publication(session, published_at=NOW - timedelta(hours=2))
    too_old = await _publication(session, published_at=NOW - timedelta(hours=100))

    pending = await PublicationRepository(session).list_for_stats(CHANNEL, SINCE, UNTIL, 100)

    assert [item.publication_id for item in pending] == [ready]
    assert too_fresh not in [item.publication_id for item in pending]
    assert too_old not in [item.publication_id for item in pending]


async def test_list_for_stats_skips_failed_other_channel_and_collected(
    session: AsyncSession,
) -> None:
    sent = await _publication(session, published_at=NOW - timedelta(hours=30))
    await _publication(
        session, published_at=NOW - timedelta(hours=30), status=PublicationStatus.FAILED
    )
    await _publication(session, published_at=NOW - timedelta(hours=30), channel_id=OTHER_CHANNEL)
    await _publication(session, published_at=NOW - timedelta(hours=30), stats_at=NOW)
    await _publication(session, published_at=None)

    pending = await PublicationRepository(session).list_for_stats(CHANNEL, SINCE, UNTIL, 100)
    assert [item.publication_id for item in pending] == [sent]


async def test_list_for_stats_orders_oldest_first_and_limits(session: AsyncSession) -> None:
    newer = await _publication(session, published_at=NOW - timedelta(hours=26))
    older = await _publication(session, published_at=NOW - timedelta(hours=48))
    repo = PublicationRepository(session)

    pending = await repo.list_for_stats(CHANNEL, SINCE, UNTIL, 100)
    assert [item.publication_id for item in pending] == [older, newer]
    assert await repo.list_for_stats(CHANNEL, SINCE, UNTIL, 1) == [
        PendingStats(publication_id=older, message_id=pending[0].message_id)
    ]


async def test_non_positive_limit_rejected(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="limit"):
        await PublicationRepository(session).list_for_stats(CHANNEL, SINCE, UNTIL, 0)


async def test_save_stats_writes_values(session: AsyncSession) -> None:
    publication_id = await _publication(session, published_at=NOW - timedelta(hours=30))
    repo = PublicationRepository(session)

    assert await repo.save_stats(publication_id, NOW, views=1500, forwards=20, reactions=7) is True

    row = await _row(session, publication_id)
    assert (row.views, row.forwards, row.reactions) == (1500, 20, 7)
    assert row.stats_at == NOW
    assert await repo.list_for_stats(CHANNEL, SINCE, UNTIL, 100) == []


async def test_save_stats_without_values_marks_only_time(session: AsyncSession) -> None:
    """Пост удалён из канала: stats_at записан, значения остаются NULL."""
    publication_id = await _publication(session, published_at=NOW - timedelta(hours=30))

    assert await PublicationRepository(session).save_stats(publication_id, NOW) is True

    row = await _row(session, publication_id)
    assert (row.views, row.forwards, row.reactions) == (None, None, None)
    assert row.stats_at == NOW


async def test_save_stats_unknown_publication(session: AsyncSession) -> None:
    assert await PublicationRepository(session).save_stats(10_000, NOW, views=1) is False


async def test_give_up_stats_marks_old_only(session: AsyncSession) -> None:
    old = await _publication(session, published_at=NOW - timedelta(hours=100))
    fresh = await _publication(session, published_at=NOW - timedelta(hours=30))
    other = await _publication(
        session, published_at=NOW - timedelta(hours=100), channel_id=OTHER_CHANNEL
    )
    repo = PublicationRepository(session)

    assert await repo.give_up_stats(CHANNEL, SINCE, NOW) == 1

    marked = await _row(session, old)
    assert marked.stats_at == NOW
    assert marked.views is None
    assert (await _row(session, fresh)).stats_at is None
    assert (await _row(session, other)).stats_at is None
    assert await repo.give_up_stats(CHANNEL, SINCE, NOW) == 0


async def test_give_up_keeps_collected_values(session: AsyncSession) -> None:
    publication_id = await _publication(session, published_at=NOW - timedelta(hours=100))
    repo = PublicationRepository(session)
    await repo.save_stats(publication_id, NOW - timedelta(hours=1), views=42)

    assert await repo.give_up_stats(CHANNEL, SINCE, NOW) == 0
    row = await _row(session, publication_id)
    assert row.views == 42
    assert row.stats_at == NOW - timedelta(hours=1)


async def test_manual_publication_counted_too(session: AsyncSession) -> None:
    """Публикация вне очереди (initiated_by) собирается так же."""
    publication_id = await _publication(session, published_at=NOW - timedelta(hours=30))
    row = await _row(session, publication_id)
    row.initiated_by = 111
    await session.flush()

    pending = await PublicationRepository(session).list_for_stats(CHANNEL, SINCE, UNTIL, 100)
    assert [item.publication_id for item in pending] == [publication_id]
