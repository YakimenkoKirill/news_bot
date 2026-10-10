from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NewsItem, ProcessedPost, Publication, Source
from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
from app.domain.enums import NewsStatus, PublicationStatus, SourceType

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CHANNEL = -1001234567890


async def _news(
    session: AsyncSession, count: int, status: NewsStatus = NewsStatus.PROCESSED
) -> list[int]:
    source = Source(type=SourceType.RSS, url="https://a.example/rss", name="A", interval_min=10)
    session.add(source)
    await session.flush()
    items = [
        NewsItem(
            source_id=source.id,
            external_id=str(i),
            url=f"https://a.example/{i}",
            title="t",
            raw_text="r",
            status=status,
        )
        for i in range(count)
    ]
    session.add_all(items)
    await session.flush()
    return [item.id for item in items]


def _post(news_id: int, cost: str = "0.001000", **overrides: object) -> ProcessedPost:
    fields: dict[str, object] = {
        "news_item_id": news_id,
        "ai_title": "Заголовок",
        "ai_summary": "Пересказ",
        "hashtags": ["#ai", "#tech"],
        "category": "tech",
        "relevance_score": 0.8,
        "prompt_version": "v1",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cost": Decimal(cost),
    }
    fields.update(overrides)
    return ProcessedPost(**fields)


async def test_add_and_read_post(session: AsyncSession) -> None:
    repo = ProcessedPostRepository(session)
    [news_id] = await _news(session, 1)
    post = await repo.add(_post(news_id))
    await session.commit()
    session.expunge_all()

    loaded = await repo.get_by_news(news_id)
    assert loaded is not None
    assert loaded.id == post.id
    assert loaded.hashtags == ["#ai", "#tech"]
    assert loaded.cost == Decimal("0.001000")
    assert isinstance(loaded.cost, Decimal)
    assert await repo.get_by_news(999_999) is None
    assert (await repo.get(post.id)) is not None


async def test_second_post_for_same_news_rejected(session: AsyncSession) -> None:
    repo = ProcessedPostRepository(session)
    [news_id] = await _news(session, 1)
    await repo.add(_post(news_id))
    with pytest.raises(IntegrityError):
        await repo.add(_post(news_id))


async def test_update_summary(session: AsyncSession) -> None:
    repo = ProcessedPostRepository(session)
    [news_id] = await _news(session, 1)
    post = await repo.add(_post(news_id))
    assert await repo.update_summary(post.id, "Новый пересказ", edited_by=42)
    assert post.ai_summary == "Новый пересказ"
    assert post.edited_by == 42
    assert not await repo.update_summary(999_999, "x", edited_by=1)


async def test_cost_since_window(session: AsyncSession) -> None:
    repo = ProcessedPostRepository(session)
    assert await repo.cost_since(NOW - timedelta(days=1)) == Decimal("0")

    ids = await _news(session, 3)
    posts = [
        await repo.add(_post(ids[0], "0.100000")),
        await repo.add(_post(ids[1], "0.020000")),
        await repo.add(_post(ids[2], "0.003000")),
    ]
    created = [NOW - timedelta(days=2), NOW - timedelta(hours=5), NOW]
    for post, at in zip(posts, created, strict=True):
        await session.execute(
            update(ProcessedPost).where(ProcessedPost.id == post.id).values(created_at=at)
        )

    total = await repo.cost_since(NOW - timedelta(days=1))
    assert total == Decimal("0.023000")
    assert isinstance(total, Decimal)
    assert await repo.cost_since(NOW + timedelta(seconds=1)) == Decimal("0")


async def test_snooze_and_due_reviews(session: AsyncSession) -> None:
    repo = ProcessedPostRepository(session)
    ids = await _news(session, 4, status=NewsStatus.PENDING_REVIEW)
    due = await repo.add(_post(ids[0]))
    future = await repo.add(_post(ids[1]))
    never = await repo.add(_post(ids[2]))
    approved = await repo.add(_post(ids[3]))
    await session.execute(
        update(NewsItem).where(NewsItem.id == ids[3]).values(status=NewsStatus.APPROVED)
    )

    assert await repo.set_snooze(due.id, NOW - timedelta(minutes=1))
    assert await repo.set_snooze(future.id, NOW + timedelta(hours=1))
    assert await repo.set_snooze(approved.id, NOW - timedelta(hours=1))
    assert [p.id for p in await repo.list_due_reviews(NOW)] == [due.id]
    assert never.snoozed_until is None

    assert await repo.set_snooze(due.id, None)
    assert await repo.list_due_reviews(NOW) == []
    assert [p.id for p in await repo.list_due_reviews(NOW + timedelta(hours=2))] == [future.id]
    assert not await repo.set_snooze(999_999, NOW)


async def _publication(session: AsyncSession) -> tuple[PublicationRepository, int]:
    [news_id] = await _news(session, 1, status=NewsStatus.APPROVED)
    post = await ProcessedPostRepository(session).add(_post(news_id))
    return PublicationRepository(session), post.id


async def test_publication_lifecycle(session: AsyncSession) -> None:
    repo, post_id = await _publication(session)
    pub = await repo.add(
        Publication(processed_post_id=post_id, channel_id=CHANNEL, status=PublicationStatus.FAILED)
    )
    assert await repo.mark_failed(pub.id, "Bad Request: chat not found")
    assert pub.error == "Bad Request: chat not found"
    assert await repo.mark_sent(pub.id, message_id=777, at=NOW)
    assert pub.status is PublicationStatus.SENT
    assert pub.message_id == 777
    assert pub.published_at == NOW
    assert pub.error is None
    assert not await repo.mark_sent(999_999, 1, NOW)


async def test_last_published_at_counts_only_sent(session: AsyncSession) -> None:
    repo, post_id = await _publication(session)
    assert await repo.last_published_at(CHANNEL) is None

    failed = await repo.add(
        Publication(processed_post_id=post_id, channel_id=CHANNEL, status=PublicationStatus.FAILED)
    )
    await session.execute(
        update(Publication).where(Publication.id == failed.id).values(published_at=NOW)
    )
    assert await repo.last_published_at(CHANNEL) is None

    for at in (NOW - timedelta(hours=3), NOW - timedelta(hours=1)):
        pub = await repo.add(
            Publication(
                processed_post_id=post_id, channel_id=CHANNEL, status=PublicationStatus.SENT
            )
        )
        await repo.mark_sent(pub.id, message_id=1, at=at)
    other = await repo.add(
        Publication(processed_post_id=post_id, channel_id=-100, status=PublicationStatus.SENT)
    )
    await repo.mark_sent(other.id, message_id=2, at=NOW + timedelta(days=1))

    assert await repo.last_published_at(CHANNEL) == NOW - timedelta(hours=1)
