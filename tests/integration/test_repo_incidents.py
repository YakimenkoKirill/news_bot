import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import Incident
from app.db.repositories.incidents import IncidentRepository
from app.domain.alerts import AlertEvent, AlertKind

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
ADMIN = 111
OTHER_ADMIN = 222
LIMIT = 200


def _event(
    kind: AlertKind = AlertKind.SOURCE_DOWN,
    key: str = "source_down:1",
    message: str = "Источник не отвечает",
    created_at: datetime = NOW,
) -> AlertEvent:
    return AlertEvent(kind=kind, key=key, message=message, created_at=created_at)


async def _count_rows(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(Incident)) or 0)


async def test_record_opens_incident(session: AsyncSession) -> None:
    incident = await IncidentRepository(session).record(_event(), LIMIT)
    assert (incident.kind, incident.key, incident.count) == (
        AlertKind.SOURCE_DOWN.value,
        "source_down:1",
        1,
    )
    assert incident.message == "Источник не отвечает"
    assert incident.first_seen_at == incident.last_seen_at == NOW
    assert (incident.resolved_at, incident.resolved_by) == (None, None)


async def test_repeat_increments_count_without_new_row(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    first = await repo.record(_event(), LIMIT)
    later = NOW + timedelta(minutes=5)
    repeated = _event(message="Источник не отвечает: 5 ошибок", created_at=later)
    second = await repo.record(repeated, LIMIT)

    assert second.id == first.id
    assert second.count == 2
    assert second.message == "Источник не отвечает: 5 ошибок"
    assert second.first_seen_at == NOW
    assert second.last_seen_at == later
    assert await _count_rows(session) == 1


async def test_different_keys_are_separate(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    await repo.record(_event(key="source_down:1"), LIMIT)
    await repo.record(_event(key="source_down:2"), LIMIT)
    assert await _count_rows(session) == 2
    assert await repo.count(open_only=True) == 2


async def test_resolved_key_opens_new_incident(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    first = await repo.record(_event(), LIMIT)
    assert await repo.resolve(first.id, ADMIN, NOW + timedelta(minutes=1)) is True

    again = await repo.record(_event(created_at=NOW + timedelta(hours=1)), LIMIT)
    assert again.id != first.id
    assert again.count == 1
    assert await _count_rows(session) == 2
    assert await repo.count(open_only=True) == 1
    assert await repo.count(open_only=False) == 2


async def test_parallel_record_gives_one_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def record() -> None:
        async with session_scope(session_factory) as session:
            await IncidentRepository(session).record(_event(), LIMIT)

    await asyncio.gather(record(), record())

    async with session_factory() as session:
        assert await _count_rows(session) == 1
        [incident] = await IncidentRepository(session).list(open_only=True, limit=10, offset=0)
        assert incident.count == 2


async def test_long_message_truncated(session: AsyncSession) -> None:
    message = "очень подробное описание ошибки " * 20
    incident = await IncidentRepository(session).record(_event(message=message), 50)
    assert len(incident.message) <= 50
    assert incident.message.endswith("…")


async def test_get(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    incident = await repo.record(_event(), LIMIT)
    found = await repo.get(incident.id)
    assert found is not None and found.id == incident.id
    assert await repo.get(10_000) is None


async def test_list_newest_first_with_pagination(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    ids = [
        (await repo.record(_event(key=f"k{i}", created_at=NOW + timedelta(minutes=i)), LIMIT)).id
        for i in range(5)
    ]
    assert [i.id for i in await repo.list(open_only=False, limit=10, offset=0)] == ids[::-1]
    assert [i.id for i in await repo.list(open_only=False, limit=2, offset=0)] == ids[:-3:-1]
    assert [i.id for i in await repo.list(open_only=False, limit=2, offset=2)] == ids[2:0:-1]
    assert await repo.list(open_only=False, limit=2, offset=99) == []


async def test_list_open_only_filters_resolved(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    closed = await repo.record(_event(key="k1"), LIMIT)
    await repo.resolve(closed.id, ADMIN, NOW)
    open_one = await repo.record(_event(key="k2", created_at=NOW + timedelta(minutes=1)), LIMIT)

    assert [i.id for i in await repo.list(open_only=True, limit=10, offset=0)] == [open_one.id]
    assert len(await repo.list(open_only=False, limit=10, offset=0)) == 2


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (-1, 0), (10, -1)])
async def test_list_invalid_arguments(session: AsyncSession, limit: int, offset: int) -> None:
    with pytest.raises(ValueError):
        await IncidentRepository(session).list(open_only=False, limit=limit, offset=offset)


async def test_record_rejects_bad_limit(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await IncidentRepository(session).record(_event(), 0)


async def test_count_open_by_kind(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    await repo.record(_event(kind=AlertKind.SOURCE_DOWN, key="a"), LIMIT)
    await repo.record(_event(kind=AlertKind.SOURCE_DOWN, key="b"), LIMIT)
    await repo.record(_event(kind=AlertKind.AI_DOWN, key="c"), LIMIT)
    closed = await repo.record(_event(kind=AlertKind.PUBLISH_ERROR, key="d"), LIMIT)
    await repo.resolve(closed.id, ADMIN, NOW)

    assert await repo.count_open_by_kind() == {
        AlertKind.SOURCE_DOWN.value: 2,
        AlertKind.AI_DOWN.value: 1,
    }


async def test_resolve_twice_returns_false(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    incident = await repo.record(_event(), LIMIT)
    at = NOW + timedelta(minutes=1)

    assert await repo.resolve(incident.id, ADMIN, at) is True
    assert await repo.resolve(incident.id, OTHER_ADMIN, at) is False
    assert await repo.resolve(10_000, ADMIN, at) is False

    stored = await repo.get(incident.id)
    assert stored is not None
    assert (stored.resolved_at, stored.resolved_by) == (at, ADMIN)


async def test_resolve_all_by_kind_leaves_others(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    await repo.record(_event(kind=AlertKind.SOURCE_DOWN, key="a"), LIMIT)
    await repo.record(_event(kind=AlertKind.SOURCE_DOWN, key="b"), LIMIT)
    other = await repo.record(_event(kind=AlertKind.AI_DOWN, key="c"), LIMIT)

    assert await repo.resolve_all(ADMIN, NOW, kind=AlertKind.SOURCE_DOWN.value) == 2
    assert [i.id for i in await repo.list(open_only=True, limit=10, offset=0)] == [other.id]
    assert await repo.resolve_all(ADMIN, NOW, kind=AlertKind.SOURCE_DOWN.value) == 0


async def test_resolve_all_closes_everything(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    await repo.record(_event(kind=AlertKind.SOURCE_DOWN, key="a"), LIMIT)
    await repo.record(_event(kind=AlertKind.AI_DOWN, key="b"), LIMIT)

    assert await repo.resolve_all(ADMIN, NOW) == 2
    assert await repo.count(open_only=True) == 0
    assert await repo.resolve_all(ADMIN, NOW) == 0


async def test_purge_resolved_keeps_open_and_recent(session: AsyncSession) -> None:
    repo = IncidentRepository(session)
    old = await repo.record(_event(key="old"), LIMIT)
    recent = await repo.record(_event(key="recent"), LIMIT)
    still_open = await repo.record(_event(key="open"), LIMIT)
    await repo.resolve(old.id, ADMIN, NOW - timedelta(days=10))
    await repo.resolve(recent.id, ADMIN, NOW)

    assert await repo.purge_resolved(NOW - timedelta(days=7)) == 1
    remaining = {i.id for i in await repo.list(open_only=False, limit=10, offset=0)}
    assert remaining == {recent.id, still_open.id}
    assert await repo.purge_resolved(NOW - timedelta(days=7)) == 0
