"""Репозитории ключевых слов, администраторов и изменяемых настроек."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, Keyword, Setting
from app.domain.enums import AdminRole, KeywordType


def normalize_keyword(word: str) -> str:
    """Приводит слово к виду для хранения и сравнения: обрезка, нижний регистр, ё -> е.

    Raises:
        ValueError: После обрезки слово пустое.
    """
    normalized = word.strip().lower().replace("ё", "е")
    if not normalized:
        raise ValueError("ключевое слово не может быть пустым")
    return normalized


class KeywordRepository:
    """Доступ к таблице keywords. Методы не коммитят."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, word: str, type: KeywordType) -> Keyword | None:
        """Добавляет нормализованное слово.

        Returns:
            Новая запись или None, если такое слово этого типа уже есть.

        Raises:
            ValueError: Слово пустое.
        """
        stmt = (
            insert(Keyword)
            .values(word=normalize_keyword(word), type=type)
            .on_conflict_do_nothing(index_elements=[Keyword.word, Keyword.type])
            .returning(Keyword)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def list_by_type(self, type: KeywordType) -> Sequence[Keyword]:
        """Возвращает слова заданного типа по алфавиту."""
        stmt = select(Keyword).where(Keyword.type == type).order_by(Keyword.word)
        return (await self._session.scalars(stmt)).all()

    async def delete(self, keyword_id: int) -> bool:
        """Удаляет слово; False, если его уже нет."""
        stmt = delete(Keyword).where(Keyword.id == keyword_id).returning(Keyword.id)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None


class AdminRepository:
    """Доступ к таблице admins. Методы не коммитят."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_admin(self, telegram_id: int) -> bool:
        """Проверяет, есть ли пользователь среди администраторов."""
        stmt = select(select(Admin.telegram_id).where(Admin.telegram_id == telegram_id).exists())
        return bool(await self._session.scalar(stmt))

    async def add(self, telegram_id: int, role: AdminRole = AdminRole.ADMIN) -> bool:
        """Добавляет администратора; False, если он уже есть (роль не меняется)."""
        stmt = (
            insert(Admin)
            .values(telegram_id=telegram_id, role=role)
            .on_conflict_do_nothing(index_elements=[Admin.telegram_id])
            .returning(Admin.telegram_id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def list(self) -> Sequence[Admin]:
        """Возвращает всех администраторов в порядке добавления."""
        stmt = select(Admin).order_by(Admin.created_at, Admin.telegram_id)
        return (await self._session.scalars(stmt)).all()


class SettingsRepository:
    """Доступ к таблице settings (значения в JSONB). Методы не коммитят."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str, default: Any = None) -> Any:
        """Возвращает значение настройки или default, если ключа нет."""
        setting = await self._session.get(Setting, key, populate_existing=True)
        return default if setting is None else setting.value

    async def set(self, key: str, value: Any) -> None:
        """Создаёт или перезаписывает настройку (INSERT ... ON CONFLICT DO UPDATE)."""
        stmt = insert(Setting).values(key=key, value=value)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": stmt.excluded.value, "updated_at": func.now()},
        )
        await self._session.execute(stmt)
