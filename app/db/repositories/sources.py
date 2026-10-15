"""Репозиторий источников новостей."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Source
from app.domain.enums import SourceType


class SourceRepository:
    """Доступ к таблице sources.

    Методы не коммитят: транзакцией управляет вызывающий код (session_scope).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, source: Source) -> Source:
        """Сохраняет новый источник и возвращает его с присвоенным id."""
        self._session.add(source)
        await self._session.flush()
        return source

    async def lock(self, source_id: int) -> bool:
        """Блокирует строку источника до конца транзакции.

        Нужен, чтобы две новости одного источника не прошли проверку лимита
        очереди одновременно: вторая подождёт коммита первой.

        Returns:
            False, если источника нет.
        """
        stmt = select(Source.id).where(Source.id == source_id).with_for_update()
        return (await self._session.scalars(stmt)).one_or_none() is not None

    async def get(self, source_id: int) -> Source | None:
        """Возвращает источник по id или None."""
        return await self._session.get(Source, source_id)

    async def list_all(self) -> Sequence[Source]:
        """Возвращает все источники (включённые и выключенные) в порядке id."""
        return (await self._session.scalars(select(Source).order_by(Source.id))).all()

    async def list_enabled(self, type: SourceType | None = None) -> Sequence[Source]:
        """Возвращает включённые источники, при необходимости только заданного типа."""
        stmt = select(Source).where(Source.enabled.is_(True)).order_by(Source.id)
        if type is not None:
            stmt = stmt.where(Source.type == type)
        return (await self._session.scalars(stmt)).all()

    async def list_due(self, now: datetime) -> Sequence[Source]:
        """Возвращает включённые источники, у которых наступило время проверки.

        Источник просрочен, если его ещё не проверяли или
        last_checked_at + interval_min минут <= now.

        Args:
            now: Текущий момент (timezone-aware).

        Returns:
            Источники в порядке id.
        """
        next_check = Source.last_checked_at + func.make_interval(0, 0, 0, 0, 0, Source.interval_min)
        stmt = (
            select(Source)
            .where(
                Source.enabled.is_(True),
                or_(Source.last_checked_at.is_(None), next_check <= now),
            )
            .order_by(Source.id)
        )
        return (await self._session.scalars(stmt)).all()

    async def mark_checked(self, source_id: int, at: datetime, fetch_state: dict[str, Any]) -> bool:
        """Запоминает время проверки и состояние сборщика (ETag, курсор и т. п.)."""
        return await self._update(source_id, last_checked_at=at, fetch_state=fetch_state)

    async def mark_item_seen(self, source_id: int, at: datetime) -> bool:
        """Сдвигает last_item_at вперёд; более раннее время его не уменьшает."""
        return await self._update(source_id, last_item_at=func.greatest(Source.last_item_at, at))

    async def register_error(self, source_id: int) -> int:
        """Атомарно увеличивает error_count (UPDATE ... RETURNING) и возвращает новое значение.

        Raises:
            sqlalchemy.exc.NoResultFound: Источника нет.
        """
        stmt = (
            update(Source)
            .where(Source.id == source_id)
            .values(error_count=Source.error_count + 1)
            .returning(Source.error_count)
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def reset_errors(self, source_id: int) -> bool:
        """Обнуляет счётчик ошибок."""
        return await self._update(source_id, error_count=0)

    async def set_enabled(self, source_id: int, enabled: bool) -> bool:
        """Включает или выключает источник."""
        return await self._update(source_id, enabled=enabled)

    async def delete(self, source_id: int) -> bool:
        """Удаляет источник; его новости удаляются каскадно в БД."""
        stmt = delete(Source).where(Source.id == source_id).returning(Source.id)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def _update(self, source_id: int, **values: Any) -> bool:
        stmt = (
            update(Source)
            .where(Source.id == source_id)
            .values(**values)
            .returning(Source)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none() is not None
