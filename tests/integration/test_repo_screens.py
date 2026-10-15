import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.repositories.screens import ScreenRepository

pytestmark = pytest.mark.integration

CHAT = 111
OTHER_CHAT = 222
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


async def test_first_screen_has_nothing_to_delete(session: AsyncSession) -> None:
    repo = ScreenRepository(session)
    assert await repo.register(CHAT, "s1", [10, 11], NOW) == []

    screen = await repo.get(CHAT)
    assert screen is not None
    assert (screen.screen_token, list(screen.message_ids)) == ("s1", [10, 11])
    assert screen.updated_at == NOW


async def test_same_screen_appends_without_deleting(session: AsyncSession) -> None:
    repo = ScreenRepository(session)
    await repo.register(CHAT, "s1", [10], NOW)

    assert await repo.register(CHAT, "s1", [11, 12], NOW + timedelta(seconds=1)) == []
    screen = await repo.get(CHAT)
    assert screen is not None
    assert list(screen.message_ids) == [10, 11, 12]


async def test_repeated_message_id_not_duplicated(session: AsyncSession) -> None:
    repo = ScreenRepository(session)
    await repo.register(CHAT, "s1", [10, 11], NOW)

    assert await repo.register(CHAT, "s1", [11, 12], NOW) == []
    screen = await repo.get(CHAT)
    assert screen is not None
    assert list(screen.message_ids) == [10, 11, 12]


async def test_new_screen_returns_previous_messages(session: AsyncSession) -> None:
    repo = ScreenRepository(session)
    await repo.register(CHAT, "s1", [10, 11], NOW)

    obsolete = await repo.register(CHAT, "s2", [20], NOW + timedelta(minutes=1))
    assert obsolete == [10, 11]
    screen = await repo.get(CHAT)
    assert screen is not None
    assert (screen.screen_token, list(screen.message_ids)) == ("s2", [20])


async def test_chats_are_independent(session: AsyncSession) -> None:
    repo = ScreenRepository(session)
    await repo.register(CHAT, "s1", [10], NOW)
    assert await repo.register(OTHER_CHAT, "s1", [20], NOW) == []

    assert await repo.register(CHAT, "s2", [11], NOW) == [10]
    other = await repo.get(OTHER_CHAT)
    assert other is not None
    assert list(other.message_ids) == [20]


async def test_get_missing_chat(session: AsyncSession) -> None:
    assert await ScreenRepository(session).get(999) is None


async def test_forget_removes_messages(session: AsyncSession) -> None:
    repo = ScreenRepository(session)
    await repo.register(CHAT, "s1", [10, 11, 12], NOW)

    await repo.forget(CHAT, [11])
    screen = await repo.get(CHAT)
    assert screen is not None
    assert list(screen.message_ids) == [10, 12]

    await repo.forget(CHAT, [10, 12])
    screen = await repo.get(CHAT)
    assert screen is not None
    assert list(screen.message_ids) == []


async def test_forget_is_safe_for_unknown(session: AsyncSession) -> None:
    repo = ScreenRepository(session)
    await repo.register(CHAT, "s1", [10], NOW)

    await repo.forget(CHAT, [])
    await repo.forget(CHAT, [999])
    await repo.forget(888, [10])
    screen = await repo.get(CHAT)
    assert screen is not None
    assert list(screen.message_ids) == [10]


async def test_forgotten_messages_not_returned_later(session: AsyncSession) -> None:
    repo = ScreenRepository(session)
    await repo.register(CHAT, "s1", [10, 11], NOW)
    await repo.forget(CHAT, [10])

    assert await repo.register(CHAT, "s2", [20], NOW) == [11]


async def test_empty_message_ids_rejected(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await ScreenRepository(session).register(CHAT, "s1", [], NOW)


async def test_parallel_screens_lose_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Два экрана одного чата параллельно: каждое сообщение либо на экране, либо на удаление."""
    async with session_scope(session_factory) as session:
        await ScreenRepository(session).register(CHAT, "s0", [1, 2], NOW)

    async def register(token: str, ids: list[int]) -> list[int]:
        async with session_scope(session_factory) as session:
            return await ScreenRepository(session).register(CHAT, token, ids, NOW)

    first, second = await asyncio.gather(register("s1", [10]), register("s2", [20]))

    async with session_factory() as session:
        screen = await ScreenRepository(session).get(CHAT)
    assert screen is not None

    accounted = set(first) | set(second) | set(screen.message_ids)
    assert {1, 2, 10, 20} <= accounted, "ни одно сообщение не потеряно"
    assert set(first) & set(second) == set(), "одно сообщение не удаляется дважды"
    assert set(screen.message_ids) & (set(first) | set(second)) == set()


async def test_parallel_same_screen_keeps_all(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def register(ids: list[int]) -> list[int]:
        async with session_scope(session_factory) as session:
            return await ScreenRepository(session).register(CHAT, "s1", ids, NOW)

    results = await asyncio.gather(register([10]), register([20]))

    assert list(results) == [[], []], "один экран — удалять нечего"
    async with session_factory() as session:
        screen = await ScreenRepository(session).get(CHAT)
    assert screen is not None
    assert set(screen.message_ids) == {10, 20}
