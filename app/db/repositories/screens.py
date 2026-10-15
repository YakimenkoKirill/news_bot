"""Репозиторий текущих экранов бота."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotScreen


class ScreenRepository:
    """Доступ к таблице bot_screens.

    Методы не коммитят: транзакцией управляет вызывающий код (session_scope).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(
        self, chat_id: int, token: str, message_ids: Sequence[int], at: datetime
    ) -> list[int]:
        """Запоминает отправленные сообщения и говорит, что пора удалить.

        Строка чата блокируется до конца транзакции, поэтому параллельные вызовы
        не теряют сообщения: каждое окажется либо в текущем экране, либо в
        возвращённом списке на удаление.

        Args:
            chat_id: Чат администратора.
            token: Номер экрана, которому принадлежат сообщения.
            message_ids: Только что отправленные сообщения.
            at: Время регистрации.

        Returns:
            Сообщения прошлого экрана, которые нужно удалить; пустой список,
            если экран тот же или это первые сообщения в чате.

        Raises:
            ValueError: Пустой список message_ids.
        """
        fresh = list(message_ids)
        if not fresh:
            raise ValueError("не передано ни одного message_id")

        current_token, current_ids = await self._lock(chat_id, token, at)
        if current_token == token:
            kept = list(current_ids)
            kept.extend(message_id for message_id in fresh if message_id not in kept)
            obsolete: list[int] = []
        else:
            kept = fresh
            obsolete = list(current_ids)
        await self._store(chat_id, token, kept, at)
        return obsolete

    async def _lock(self, chat_id: int, token: str, at: datetime) -> tuple[str, list[int]]:
        """Создаёт строку чата, если её нет, и блокирует её, возвращая текущее состояние.

        Обычный SELECT ... FOR UPDATE нечего блокировать, когда строки ещё нет,
        поэтому блокировка берётся через INSERT ... ON CONFLICT DO UPDATE: обновление
        ничего не меняет, но держит строку до конца транзакции.
        """
        stmt = (
            insert(BotScreen)
            .values(chat_id=chat_id, screen_token=token, message_ids=[], updated_at=at)
            .on_conflict_do_update(
                index_elements=[BotScreen.chat_id],
                set_={"screen_token": BotScreen.screen_token},
            )
            .returning(BotScreen.screen_token, BotScreen.message_ids)
        )
        row = (await self._session.execute(stmt)).one()
        return str(row[0]), list(row[1])

    async def _store(
        self, chat_id: int, token: str, message_ids: Sequence[int], at: datetime
    ) -> None:
        stmt = (
            update(BotScreen)
            .where(BotScreen.chat_id == chat_id)
            .values(screen_token=token, message_ids=list(message_ids), updated_at=at)
            .execution_options(populate_existing=True)
        )
        await self._session.execute(stmt)

    async def get(self, chat_id: int) -> BotScreen | None:
        """Текущий экран чата или None."""
        return await self._session.get(BotScreen, chat_id, populate_existing=True)

    async def forget(self, chat_id: int, message_ids: Sequence[int]) -> None:
        """Убирает сообщения из текущего экрана: их уже нет в чате."""
        gone = set(message_ids)
        if not gone:
            return
        screen = await self.get(chat_id)
        if screen is None:
            return
        kept = [message_id for message_id in screen.message_ids if message_id not in gone]
        if kept != list(screen.message_ids):
            screen.message_ids = kept
