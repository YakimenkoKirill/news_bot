"""Репозиторий журнала ошибок."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Incident
from app.domain.alerts import AlertEvent
from app.pipeline.ai.prompts import truncate_words

OPEN_ONLY = text("resolved_at IS NULL")


class IncidentRepository:
    """Доступ к таблице incidents.

    Методы не коммитят: транзакцией управляет вызывающий код (session_scope).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, event: AlertEvent, max_message_chars: int) -> Incident:
        """Записывает событие: открывает инцидент или наращивает счётчик открытого.

        Один запрос INSERT ... ON CONFLICT по частичному уникальному индексу,
        поэтому параллельные события с одним key дают одну строку.

        Args:
            event: Событие алерта.
            max_message_chars: До скольких символов обрезать текст сообщения.

        Returns:
            Открытый инцидент после записи.

        Raises:
            ValueError: max_message_chars <= 0.
        """
        if max_message_chars <= 0:
            raise ValueError(
                f"max_message_chars должен быть положительным, получено {max_message_chars}"
            )
        message = truncate_words(event.message, max_message_chars)
        stmt = (
            insert(Incident)
            .values(
                kind=event.kind.value,
                key=event.key,
                message=message,
                count=1,
                first_seen_at=event.created_at,
                last_seen_at=event.created_at,
            )
            .on_conflict_do_update(
                index_elements=[Incident.key],
                index_where=OPEN_ONLY,
                set_={
                    "count": Incident.count + 1,
                    "message": message,
                    "last_seen_at": event.created_at,
                },
            )
            .returning(Incident)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one()

    async def get(self, incident_id: int) -> Incident | None:
        """Инцидент по id или None."""
        return await self._session.get(Incident, incident_id)

    async def list(self, open_only: bool, limit: int, offset: int) -> list[Incident]:
        """Инциденты, самые свежие первыми.

        Raises:
            ValueError: limit <= 0 или offset < 0.
        """
        if limit <= 0:
            raise ValueError(f"limit должен быть положительным, получено {limit}")
        if offset < 0:
            raise ValueError(f"offset не может быть отрицательным, получено {offset}")
        stmt = (
            select(Incident)
            .order_by(Incident.last_seen_at.desc(), Incident.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if open_only:
            stmt = stmt.where(Incident.resolved_at.is_(None))
        return list((await self._session.scalars(stmt)).all())

    async def count(self, open_only: bool) -> int:
        """Число инцидентов: всех или только открытых."""
        stmt = select(func.count()).select_from(Incident)
        if open_only:
            stmt = stmt.where(Incident.resolved_at.is_(None))
        return int(await self._session.scalar(stmt) or 0)

    async def count_open_by_kind(self) -> dict[str, int]:
        """Число открытых инцидентов по видам (виды без открытых не попадают)."""
        stmt = (
            select(Incident.kind, func.count())
            .where(Incident.resolved_at.is_(None))
            .group_by(Incident.kind)
        )
        return {kind: count for kind, count in (await self._session.execute(stmt)).all()}

    async def resolve(self, incident_id: int, by: int, at: datetime) -> bool:
        """Закрывает открытый инцидент.

        Returns:
            True, если инцидент был открыт и закрылся; False, если его нет или он уже закрыт.
        """
        stmt = (
            update(Incident)
            .where(Incident.id == incident_id, Incident.resolved_at.is_(None))
            .values(resolved_at=at, resolved_by=by)
            .returning(Incident.id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none() is not None

    async def resolve_all(self, by: int, at: datetime, kind: str | None = None) -> int:
        """Закрывает все открытые инциденты (или только одного вида).

        Returns:
            Сколько инцидентов закрыто.
        """
        stmt = (
            update(Incident)
            .where(Incident.resolved_at.is_(None))
            .values(resolved_at=at, resolved_by=by)
            .returning(Incident.id)
            .execution_options(populate_existing=True)
        )
        if kind is not None:
            stmt = stmt.where(Incident.kind == kind)
        return len((await self._session.scalars(stmt)).all())

    async def purge_resolved(self, before: datetime) -> int:
        """Удаляет закрытые инциденты с resolved_at < before; открытые не трогает.

        Returns:
            Сколько строк удалено.
        """
        stmt = (
            delete(Incident)
            .where(Incident.resolved_at.is_not(None), Incident.resolved_at < before)
            .returning(Incident.id)
        )
        return len((await self._session.scalars(stmt)).all())
