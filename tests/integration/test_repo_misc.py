from datetime import UTC, datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting
from app.db.repositories.misc import (
    AdminRepository,
    KeywordRepository,
    SettingsRepository,
    normalize_keyword,
)
from app.domain.enums import AdminRole, KeywordType

pytestmark = pytest.mark.integration


async def test_keyword_normalization(session: AsyncSession) -> None:
    repo = KeywordRepository(session)
    keyword = await repo.add("  Ёлка ", KeywordType.INCLUDE)
    assert keyword is not None
    assert keyword.word == "елка"
    assert keyword.type is KeywordType.INCLUDE


async def test_keyword_duplicates_ignored(session: AsyncSession) -> None:
    repo = KeywordRepository(session)
    assert await repo.add("ИИ", KeywordType.INCLUDE) is not None
    assert await repo.add(" ии ", KeywordType.INCLUDE) is None
    assert await repo.add("ии", KeywordType.EXCLUDE) is not None
    assert [k.word for k in await repo.list_by_type(KeywordType.INCLUDE)] == ["ии"]
    assert [k.word for k in await repo.list_by_type(KeywordType.EXCLUDE)] == ["ии"]


async def test_keyword_empty_rejected(session: AsyncSession) -> None:
    repo = KeywordRepository(session)
    for word in ("", "   "):
        with pytest.raises(ValueError, match="пуст"):
            await repo.add(word, KeywordType.EXCLUDE)


async def test_keyword_list_sorted_and_delete(session: AsyncSession) -> None:
    repo = KeywordRepository(session)
    for word in ("робот", "автопилот", "нейросеть"):
        await repo.add(word, KeywordType.INCLUDE)
    listed = await repo.list_by_type(KeywordType.INCLUDE)
    assert [k.word for k in listed] == ["автопилот", "нейросеть", "робот"]

    assert await repo.delete(listed[0].id)
    assert not await repo.delete(listed[0].id)
    assert [k.word for k in await repo.list_by_type(KeywordType.INCLUDE)] == ["нейросеть", "робот"]


def test_normalize_keyword() -> None:
    assert normalize_keyword(" ЁЖИК ") == "ежик"


async def test_admins(session: AsyncSession) -> None:
    repo = AdminRepository(session)
    assert not await repo.is_admin(111)
    assert await repo.add(111, AdminRole.OWNER)
    assert await repo.add(222)
    assert not await repo.add(111, AdminRole.ADMIN)
    assert await repo.is_admin(111)
    assert not await repo.is_admin(333)
    admins = {admin.telegram_id: admin.role for admin in await repo.list()}
    assert admins == {111: AdminRole.OWNER, 222: AdminRole.ADMIN}


async def test_settings_default_and_upsert(session: AsyncSession) -> None:
    repo = SettingsRepository(session)
    assert await repo.get("paused") is None
    assert await repo.get("paused", default=False) is False

    await repo.set("paused", True)
    assert await repo.get("paused", default=False) is True

    past = datetime(2020, 1, 1, tzinfo=UTC)
    await session.execute(update(Setting).where(Setting.key == "paused").values(updated_at=past))
    await repo.set("paused", {"until": "2026-10-11T00:00:00+00:00", "by": 111})
    assert await repo.get("paused") == {"until": "2026-10-11T00:00:00+00:00", "by": 111}
    setting = await session.get(Setting, "paused", populate_existing=True)
    assert setting is not None
    assert setting.updated_at > past

    await repo.set("limits", [1, 2, 3])
    assert await repo.get("limits") == [1, 2, 3]
