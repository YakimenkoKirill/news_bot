"""Middleware бота управления."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.bot import texts
from app.config import Settings

log = structlog.get_logger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class AdminChecker(Protocol):
    """Проверка администратора в БД (AdminRepository через сессию)."""

    async def is_admin(self, telegram_id: int) -> bool:
        """Есть ли пользователь среди администраторов."""
        raise NotImplementedError


def _is_callback(event: Any) -> bool:
    """CallbackQuery отличается полем chat_instance."""
    return hasattr(event, "chat_instance")


class AdminOnlyMiddleware(BaseMiddleware):
    """Пропускает к хендлерам только администраторов.

    Администратор — пользователь из telegram_admin_ids или из таблицы admins.
    Остальным отправляется отказ не чаще раза в refusal_cooldown_s; апдейты без
    пользователя (например, посты канала) отбрасываются молча.
    """

    def __init__(
        self,
        settings: Settings,
        admin_repo: AdminChecker,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._admin_ids = frozenset(settings.telegram_admin_ids)
        self._admin_repo = admin_repo
        self._cooldown = settings.bot.refusal_cooldown_s
        self._clock = clock
        self._last_refusal: dict[int, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Вызывает хендлер для администратора, иначе отказывает и прерывает обработку."""
        user = getattr(event, "from_user", None)
        if user is None:
            return None
        if user.id in self._admin_ids or await self._admin_repo.is_admin(user.id):
            return await handler(event, data)
        await self._refuse(event, user.id)
        return None

    async def _refuse(self, event: Any, user_id: int) -> None:
        now = self._clock()
        last = self._last_refusal.get(user_id)
        should_reply = last is None or now - last >= self._cooldown
        log.warning("bot.access_denied", user_id=user_id, replied=should_reply)
        if should_reply:
            self._last_refusal[user_id] = now
        if _is_callback(event):
            text = texts.ACCESS_DENIED if should_reply else None
            await event.answer(text, show_alert=should_reply)
        elif should_reply:
            await event.answer(texts.ACCESS_DENIED)
