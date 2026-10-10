from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NewsItem, Source
from app.db.repositories.news import NewsRepository
from app.domain.enums import NewsStatus, SourceType

pytestmark = pytest.mark.integration

PUBLISHED = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Raw:
    source_id: int
    external_id: str
    url: str = "https://a.example/news/1"
    title: str = "Заголовок"
    text: str = "Текст новости"
    image_url: str | None = None
    published_at: datetime | None = PUBLISHED
    raw_meta: Mapping[str, Any] = field(default_factory=dict)


async def _source(session: AsyncSession, url: str = "https://a.example/rss") -> int:
    source = Source(type=SourceType.RSS, url=url, name="A", interval_min=10)
    session.add(source)
    await session.flush()
    return source.id


async def _count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(NewsItem)) or 0)


async def _set(session: AsyncSession, item_id: int, **values: Any) -> None:
    await session.execute(update(NewsItem).where(NewsItem.id == item_id).values(**values))


async def test_add_if_new_inserts_once(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    source_id = await _source(session)
    raw = Raw(source_id, "ext-1", image_url="https://a.example/i.jpg", raw_meta={"k": "v"})

    item = await repo.add_if_new(raw)
    assert item is not None
    assert item.id is not None
    assert item.raw_text == "Текст новости"
    assert item.status is NewsStatus.NEW
    assert item.published_at == PUBLISHED
    assert item.raw_meta == {"k": "v"}

    assert await repo.add_if_new(raw) is None
    assert await repo.add_if_new(Raw(source_id, "ext-1", title="Другой")) is None
    assert await _count(session) == 1

    other_source = await _source(session, "https://b.example/rss")
    assert await repo.add_if_new(Raw(other_source, "ext-1")) is not None
    assert await _count(session) == 2


async def test_exists_url(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    assert not await repo.exists_url("https://a.example/news/1")
    item = await repo.add_if_new(Raw(await _source(session), "1"))
    assert item is not None
    await _set(session, item.id, canonical_url="https://a.example/news/1")
    assert await repo.exists_url("https://a.example/news/1")
    assert not await repo.exists_url("https://a.example/news/2")


async def test_find_by_hash_respects_window(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    now = datetime.now(UTC)
    assert await repo.find_by_hash("h1", now - timedelta(days=1)) is None

    source_id = await _source(session)
    old = await repo.add_if_new(Raw(source_id, "old"))
    fresh = await repo.add_if_new(Raw(source_id, "fresh"))
    newer = await repo.add_if_new(Raw(source_id, "newer"))
    assert old is not None
    assert fresh is not None
    assert newer is not None
    await _set(session, old.id, content_hash="h1", created_at=now - timedelta(days=3))
    await _set(session, fresh.id, content_hash="h1", created_at=now - timedelta(hours=2))
    await _set(session, newer.id, content_hash="h1", created_at=now - timedelta(hours=1))

    found = await repo.find_by_hash("h1", now - timedelta(days=1))
    assert found is not None
    assert found.id == fresh.id
    assert await repo.find_by_hash("h1", now) is None
    assert await repo.find_by_hash("h2", now - timedelta(days=7)) is None


async def test_recent_simhashes(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    now = datetime.now(UTC)
    assert await repo.recent_simhashes(now - timedelta(days=1)) == []

    source_id = await _source(session)
    a = await repo.add_if_new(Raw(source_id, "a"))
    b = await repo.add_if_new(Raw(source_id, "b"))
    old = await repo.add_if_new(Raw(source_id, "old"))
    await repo.add_if_new(Raw(source_id, "no-hash"))
    assert a is not None
    assert b is not None
    assert old is not None
    negative = -(2**63)
    await _set(session, a.id, simhash=negative)
    await _set(session, b.id, simhash=2**63 - 1, cluster_id=a.id)
    await _set(session, old.id, simhash=1, created_at=now - timedelta(days=5))

    assert await repo.recent_simhashes(now - timedelta(days=1)) == [
        (a.id, negative, None),
        (b.id, 2**63 - 1, a.id),
    ]


async def test_max_tg_message_id_compares_numbers(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    source_id = await _source(session)
    other_id = await _source(session, "https://t.me/other")
    assert await repo.max_tg_message_id(source_id) is None

    for ext, msg_id in (("1", "9"), ("2", "10"), ("3", "100"), ("4", "99")):
        await repo.add_if_new(Raw(source_id, ext, raw_meta={"msg_id": msg_id}))
    await repo.add_if_new(Raw(source_id, "5", raw_meta={}))
    await repo.add_if_new(Raw(source_id, "6", raw_meta={"msg_id": 1000}))
    await repo.add_if_new(Raw(other_id, "7", raw_meta={"msg_id": "5000"}))

    assert await repo.max_tg_message_id(source_id) == 1000
    assert await repo.max_tg_message_id(other_id) == 5000


async def test_find_by_url_and_exclude_self(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    now = datetime.now(UTC)
    source_id = await _source(session)
    first = await repo.add_if_new(Raw(source_id, "u1"))
    second = await repo.add_if_new(Raw(source_id, "u2"))
    old = await repo.add_if_new(Raw(source_id, "u-old"))
    assert first is not None
    assert second is not None
    assert old is not None
    url = "https://a.example/news/1"
    await _set(session, first.id, canonical_url=url, content_hash="h")
    await _set(session, second.id, canonical_url=url, content_hash="h")
    await _set(
        session, old.id, canonical_url=url, content_hash="h", created_at=now - timedelta(days=5)
    )
    since = now - timedelta(days=1)

    found = await repo.find_by_url(url, since)
    assert found is not None
    assert found.id == first.id
    excluded = await repo.find_by_url(url, since, exclude_id=first.id)
    assert excluded is not None
    assert excluded.id == second.id
    assert await repo.find_by_url("https://a.example/other", since) is None

    by_hash = await repo.find_by_hash("h", since, exclude_id=first.id)
    assert by_hash is not None
    assert by_hash.id == second.id


async def test_get_by_id(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    item = await repo.add_if_new(Raw(await _source(session), "g1"))
    assert item is not None
    found = await repo.get(item.id)
    assert found is not None
    assert found.external_id == "g1"
    assert await repo.get(999_999) is None
