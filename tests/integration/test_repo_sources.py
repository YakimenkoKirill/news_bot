import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import NewsItem, Source
from app.db.repositories.sources import SourceRepository
from app.domain.enums import ModerationMode, SourceType

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _source(url: str, **overrides: object) -> Source:
    fields: dict[str, object] = {
        "type": SourceType.RSS,
        "url": url,
        "name": url,
        "interval_min": 10,
    }
    fields.update(overrides)
    return Source(**fields)


async def test_add_and_get_with_defaults(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    source = await repo.add(_source("https://a.example/rss"))
    await session.commit()
    session.expunge_all()

    loaded = await repo.get(source.id)
    assert loaded is not None
    assert loaded.url == "https://a.example/rss"
    assert loaded.enabled is True
    assert loaded.moderation_mode is ModerationMode.AUTO
    assert loaded.fetch_state == {}
    assert loaded.error_count == 0
    assert loaded.created_at.tzinfo is not None
    assert await repo.get(9999) is None


async def test_list_enabled_filters_by_type(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    rss = await repo.add(_source("https://a.example/rss"))
    web = await repo.add(_source("https://b.example", type=SourceType.WEB))
    await repo.add(_source("https://c.example/rss", enabled=False))

    assert [s.id for s in await repo.list_enabled()] == [rss.id, web.id]
    assert [s.id for s in await repo.list_enabled(SourceType.WEB)] == [web.id]
    assert await repo.list_enabled(SourceType.TG) == []


async def test_list_due_on_interval_boundary(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    interval = timedelta(minutes=10)
    never = await repo.add(_source("https://never.example"))
    exact = await repo.add(_source("https://exact.example", last_checked_at=NOW - interval))
    early = await repo.add(
        _source("https://early.example", last_checked_at=NOW - interval + timedelta(seconds=1))
    )
    overdue = await repo.add(_source("https://late.example", last_checked_at=NOW - 6 * interval))
    await repo.add(_source("https://off.example", enabled=False))
    await repo.add(
        _source("https://off-old.example", enabled=False, last_checked_at=NOW - 6 * interval)
    )

    due = {s.id for s in await repo.list_due(NOW)}
    assert due == {never.id, exact.id, overdue.id}
    assert early.id not in due


async def test_mark_checked_and_item_seen(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    source = await repo.add(_source("https://a.example/rss"))

    assert await repo.mark_checked(source.id, NOW, {"etag": "abc"})
    assert await repo.mark_item_seen(source.id, NOW)
    assert await repo.mark_item_seen(source.id, NOW - timedelta(hours=1))
    await session.commit()
    session.expunge_all()

    loaded = await repo.get(source.id)
    assert loaded is not None
    assert loaded.last_checked_at == NOW
    assert loaded.fetch_state == {"etag": "abc"}
    assert loaded.last_item_at == NOW
    assert not await repo.mark_checked(9999, NOW, {})


async def test_errors_enable_and_delete(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    source = await repo.add(_source("https://a.example/rss"))

    assert await repo.register_error(source.id) == 1
    assert await repo.register_error(source.id) == 2
    assert source.error_count == 2
    assert await repo.reset_errors(source.id)
    assert source.error_count == 0

    assert await repo.set_enabled(source.id, False)
    assert source.enabled is False
    assert await repo.list_enabled() == []

    with pytest.raises(NoResultFound):
        await repo.register_error(9999)


async def test_delete_cascades_news(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    source = await repo.add(_source("https://a.example/rss"))
    session.add(
        NewsItem(
            source_id=source.id, external_id="1", url="https://a.example/1", title="t", raw_text="r"
        )
    )
    await session.commit()

    assert await repo.delete(source.id)
    assert not await repo.delete(source.id)
    await session.commit()
    session.expunge_all()

    assert await repo.get(source.id) is None
    assert await session.scalar(select(func.count()).select_from(NewsItem)) == 0


async def test_register_error_is_atomic_under_concurrency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        source_id = (await SourceRepository(session).add(_source("https://a.example/rss"))).id

    async def bump() -> int:
        async with session_scope(session_factory) as session:
            return await SourceRepository(session).register_error(source_id)

    calls = 20
    results = await asyncio.gather(*(bump() for _ in range(calls)))
    assert sorted(results) == list(range(1, calls + 1))

    async with session_scope(session_factory) as session:
        source = await SourceRepository(session).get(source_id)
        assert source is not None
        assert source.error_count == calls


async def test_list_all_includes_disabled(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    first = await repo.add(_source("https://a.example/rss"))
    second = await repo.add(_source("https://b.example/rss", enabled=False))
    assert [s.id for s in await repo.list_all()] == [first.id, second.id]
