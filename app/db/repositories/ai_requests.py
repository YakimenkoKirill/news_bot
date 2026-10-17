"""Репозиторий запросов к модели."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AiRequest


class AiRequestRepository:
    """Доступ к таблице ai_requests. Методы не коммитят."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, request: AiRequest) -> AiRequest:
        """Сохраняет запрос к модели и возвращает его с присвоенным id."""
        self._session.add(request)
        await self._session.flush()
        return request

    async def cost_since(self, dt: datetime) -> Decimal:
        """Суммирует стоимость запросов к модели, сделанных начиная с dt.

        Считаются и неудачные попытки: они тоже оплачены.

        Args:
            dt: Начало окна (timezone-aware).

        Returns:
            Сумма в долларах; 0, если запросов не было.
        """
        stmt = select(func.coalesce(func.sum(AiRequest.cost), 0)).where(AiRequest.created_at >= dt)
        return Decimal((await self._session.scalar(stmt)) or 0)
