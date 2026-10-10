from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import NewsItem, Source
from app.db.repositories.news import NewsRepository, StaleStatusError
from app.domain.enums import NewsStatus, SourceType
from app.domain.status import InvalidTransition

pytestmark = pytest.mark.integration

S = NewsStatus


async def _seed(session: AsyncSession, count: int, status: NewsStatus = S.NEW) -> list[int]:
    source = Source(type=SourceType.RSS, url="https://a.example/rss", name="A", interval_min=10)
    session.add(source)
    await session.flush()
    base = datetime.now(UTC) - timedelta(hours=1)
    items = [
        NewsItem(
            source_id=source.id,
            external_id=str(i),
            url=f"https://a.example/{i}",
            title="t",
            raw_text="r",
            status=status,
            created_at=base + timedelta(minutes=i),
        )
        for i in range(count)
    ]
    session.add_all(items)
    await session.flush()
    return [item.id for item in items]


async def _get(session: AsyncSession, news_id: int) -> NewsItem:
    item = await session.get(NewsItem, news_id, populate_existing=True)
    assert item is not None
    return item


async def test_set_status_valid_transition(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    [news_id] = await _seed(session, 1)
    await repo.set_status(news_id, S.NEW, S.FILTERED, reason="too_old")
    item = await _get(session, news_id)
    assert item.status is S.FILTERED
    assert item.reject_reason == "too_old"


async def test_set_status_rejects_invalid_transition(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    [news_id] = await _seed(session, 1)
    with pytest.raises(InvalidTransition):
        await repo.set_status(news_id, S.NEW, S.PUBLISHED)
    assert (await _get(session, news_id)).status is S.NEW


async def test_set_status_rejects_stale_expected(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    [news_id] = await _seed(session, 1)
    await repo.set_status(news_id, S.NEW, S.NORMALIZED)
    with pytest.raises(StaleStatusError) as caught:
        await repo.set_status(news_id, S.NEW, S.DUPLICATE)
    assert caught.value.news_id == news_id
    assert (await _get(session, news_id)).status is S.NORMALIZED
    with pytest.raises(StaleStatusError):
        await repo.set_status(999_999, S.NEW, S.NORMALIZED)


async def test_claim_batch_order_and_limits(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    assert await repo.claim_batch(S.NEW, 5) == []
    ids = await _seed(session, 4)
    claimed = await repo.claim_batch(S.NEW, 3)
    assert [item.id for item in claimed] == ids[:3]
    assert await repo.claim_batch(S.NORMALIZED, 3) == []
    for bad in (0, -1):
        with pytest.raises(ValueError, match="limit"):
            await repo.claim_batch(S.NEW, bad)


async def test_parallel_claims_are_disjoint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        ids = await _seed(session, 5)

    async with (
        session_factory() as first,
        session_factory() as second,
        first.begin(),
        second.begin(),
    ):
        batch_a = await NewsRepository(first).claim_batch(S.NEW, 3)
        batch_b = await NewsRepository(second).claim_batch(S.NEW, 10)
        a = {item.id for item in batch_a}
        b = {item.id for item in batch_b}
        assert a == set(ids[:3])
        assert b == set(ids[3:])
        assert not a & b


async def test_update_fields(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    [news_id] = await _seed(session, 1)
    fields: dict[str, Any] = {
        "canonical_url": "https://a.example/0",
        "clean_text": "чистый",
        "language": "ru",
        "content_hash": "abc",
        "simhash": -5,
    }
    assert await repo.update_fields(news_id, **fields)
    item = await _get(session, news_id)
    for key, value in fields.items():
        assert getattr(item, key) == value
    assert not await repo.update_fields(999_999, language="en")
    with pytest.raises(ValueError, match="status"):
        await repo.update_fields(news_id, status=S.PUBLISHED)
    with pytest.raises(ValueError):
        await repo.update_fields(news_id)


async def test_updated_at_changes_on_status_update(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    [news_id] = await _seed(session, 1)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    await session.execute(update(NewsItem).where(NewsItem.id == news_id).values(updated_at=past))
    await repo.set_status(news_id, S.NEW, S.NORMALIZED)
    assert (await _get(session, news_id)).updated_at > past


async def test_count_by_status_has_all_keys(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    assert await repo.count_by_status() == {status: 0 for status in NewsStatus}
    await _seed(session, 2)
    [failed_id] = await _seed_more(session)
    counts = await repo.count_by_status()
    assert set(counts) == set(NewsStatus)
    assert counts[S.NEW] == 2
    assert counts[S.FAILED] == 1
    assert counts[S.PUBLISHED] == 0
    assert failed_id > 0


async def _seed_more(session: AsyncSession) -> list[int]:
    source = Source(type=SourceType.TG, url="https://t.me/x", name="X", interval_min=5)
    session.add(source)
    await session.flush()
    item = NewsItem(
        source_id=source.id,
        external_id="f",
        url="https://t.me/x/1",
        title="t",
        raw_text="r",
        status=S.FAILED,
    )
    session.add(item)
    await session.flush()
    return [item.id]


async def test_claim_batch_multiple_statuses(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    ids = await _seed(session, 4, status=S.APPROVED)
    await repo.set_status(ids[0], S.APPROVED, S.SCHEDULED)
    await repo.set_status(ids[1], S.APPROVED, S.PUBLISHED)
    claimed = await repo.claim_batch([S.APPROVED, S.SCHEDULED], 10)
    assert [item.id for item in claimed] == [ids[0], ids[2], ids[3]]
    with pytest.raises(ValueError, match="статус"):
        await repo.claim_batch([], 1)
