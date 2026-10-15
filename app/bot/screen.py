"""Один экран в чате: новое сообщение бота убирает прошлое.

Экран — группа сообщений, отправленных в ответ на одно событие. Номер текущего
экрана живёт в ContextVar, а отправки перехватываются middleware сессии aiogram,
поэтому обработчики ничего не знают об экранах и не меняются.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Protocol

import structlog
from aiogram import BaseMiddleware, Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    SendAnimation,
    SendDocument,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SendVideo,
)
from aiogram.methods.base import Response, TelegramMethod
from aiogram.types import Message, TelegramObject

from app.config import UiConfig

log = structlog.get_logger(__name__)

_SCREEN: ContextVar[str | None] = ContextVar("bot_screen", default=None)

TRACKED_METHODS = (
    SendMessage,
    SendPhoto,
    SendDocument,
    SendAnimation,
    SendVideo,
    SendMediaGroup,
)
SKIP_DELETE_ERRORS = ("message to delete not found", "message can't be deleted")


@contextmanager
def new_screen() -> Iterator[str]:
    """Открывает новый экран: всё отправленное внутри принадлежит ему."""
    token = uuid.uuid4().hex
    reset = _SCREEN.set(token)
    try:
        yield token
    finally:
        _SCREEN.reset(reset)


def current_screen() -> str:
    """Номер текущего экрана.

    Отправка вне new_screen получает одноразовый номер, то есть сама считается
    новым экраном и убирает прошлый.
    """
    token = _SCREEN.get()
    return token if token is not None else uuid.uuid4().hex


class ScreenRepo(Protocol):
    """Нужная трекеру часть ScreenRepository."""

    async def register(
        self, chat_id: int, token: str, message_ids: Sequence[int], at: datetime
    ) -> list[int]:
        """Запоминает сообщения экрана и возвращает те, что пора удалить."""
        raise NotImplementedError

    async def forget(self, chat_id: int, message_ids: Sequence[int]) -> None:
        """Убирает сообщения из текущего экрана."""
        raise NotImplementedError


class ScreenTracker:
    """Хранит состав экрана и удаляет сообщения прошлого.

    Отправки в один чат обрабатываются по очереди, в разные — независимо.
    Ошибки удаления не пробрасываются: смена экрана не должна ломать отправку.
    """

    def __init__(
        self,
        session_scope: Callable[[], AbstractAsyncContextManager[Any]],
        screen_repo: Callable[[Any], ScreenRepo],
        bot: Any,
        cfg: UiConfig,
        clock: Callable[[], datetime],
    ) -> None:
        self._scope = session_scope
        self._repo = screen_repo
        self._bot = bot
        self._cfg = cfg
        self._clock = clock
        self._locks: dict[int, asyncio.Lock] = {}

    async def on_sent(self, chat_id: int, message_ids: Sequence[int], token: str) -> None:
        """Регистрирует отправленные сообщения и убирает прошлый экран."""
        if not message_ids:
            return
        async with self._locks.setdefault(chat_id, asyncio.Lock()):
            try:
                async with self._scope() as session:
                    obsolete = await self._repo(session).register(
                        chat_id, token, list(message_ids), self._clock()
                    )
            except Exception:
                log.exception("screen.register_failed", chat_id=chat_id)
                return
            if obsolete:
                await self._delete(chat_id, obsolete)

    async def _delete(self, chat_id: int, message_ids: Sequence[int]) -> None:
        size = self._cfg.delete_batch_size
        gone: list[int] = []
        for start in range(0, len(message_ids), size):
            batch = list(message_ids[start : start + size])
            try:
                await self._bot.delete_messages(chat_id=chat_id, message_ids=batch)
            except TelegramBadRequest:
                gone.extend(await self._delete_one_by_one(chat_id, batch))
            except Exception:
                log.exception("screen.delete_failed", chat_id=chat_id)
            else:
                gone.extend(batch)
        if gone:
            await self._forget(chat_id, gone)

    async def _delete_one_by_one(self, chat_id: int, message_ids: Sequence[int]) -> list[int]:
        """Удаляет по одному, когда пачка не прошла; возвращает выбывшие сообщения."""
        gone: list[int] = []
        for message_id in message_ids:
            try:
                await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramBadRequest as exc:
                if not _is_already_gone(exc):
                    log.exception("screen.delete_failed", chat_id=chat_id, message_id=message_id)
                    continue
                log.info("screen.delete_skipped", chat_id=chat_id, message_id=message_id)
            except Exception:
                log.exception("screen.delete_failed", chat_id=chat_id, message_id=message_id)
                continue
            gone.append(message_id)
        return gone

    async def _forget(self, chat_id: int, message_ids: Sequence[int]) -> None:
        try:
            async with self._scope() as session:
                await self._repo(session).forget(chat_id, message_ids)
        except Exception:
            log.exception("screen.forget_failed", chat_id=chat_id)


def _is_already_gone(error: TelegramBadRequest) -> bool:
    """Сообщение удалено вручную или старше 48 часов — это не ошибка."""
    message = error.message.lower()
    return any(known in message for known in SKIP_DELETE_ERRORS)


def sent_message_ids(result: Any) -> list[int]:
    """Id отправленных сообщений: у альбома их несколько."""
    if isinstance(result, Message):
        return [result.message_id]
    if isinstance(result, list):
        return [item.message_id for item in result if isinstance(item, Message)]
    return []


class ScreenRequestMiddleware(BaseRequestMiddleware):
    """Перехватывает исходящие сообщения бота и меняет экран в личных чатах.

    Канал и группы не трогаются никогда: посты в канале удаляться не должны.
    """

    def __init__(self, tracker: ScreenTracker, channel_id: int, cfg: UiConfig) -> None:
        self._tracker = tracker
        self._channel_id = channel_id
        self._cfg = cfg

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Response[Any]:
        """Отправляет запрос и регистрирует сообщение в текущем экране."""
        response = await make_request(bot, method)
        if not self._cfg.single_screen or not isinstance(method, TRACKED_METHODS):
            return response
        chat_id = _private_chat_id(method, self._channel_id)
        if chat_id is None:
            return response
        message_ids = sent_message_ids(response.result)
        if message_ids:
            await self._tracker.on_sent(chat_id, message_ids, current_screen())
        return response


def _private_chat_id(method: TelegramMethod[Any], channel_id: int) -> int | None:
    """Id личного чата получателя или None для канала, групп и неизвестных чатов."""
    raw = getattr(method, "chat_id", None)
    if not isinstance(raw, int) or raw <= 0 or raw == channel_id:
        return None
    return raw


class ScreenUpdateMiddleware(BaseMiddleware):
    """Открывает экран на каждое действие администратора и убирает его сообщения.

    Всё, что бот отправит в ответ на команду или кнопку, попадает в один экран и
    заменяет прошлый. Сообщение администратора удаляется после обработки — в том
    числе когда обработчик ничего не ответил или упал.
    """

    def __init__(self, cfg: UiConfig) -> None:
        self._cfg = cfg

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Выполняет обработчик внутри нового экрана."""
        with new_screen():
            try:
                return await handler(event, data)
            finally:
                if isinstance(event, Message) and self._should_delete(event):
                    await self._delete_incoming(event)

    def _should_delete(self, message: Message) -> bool:
        if not (self._cfg.single_screen and self._cfg.delete_user_messages):
            return False
        return message.chat.type == ChatType.PRIVATE

    @staticmethod
    async def _delete_incoming(message: Message) -> None:
        """Убирает сообщение администратора; не вышло — оставляем как есть."""
        try:
            await message.delete()
        except TelegramBadRequest as exc:
            log.info("screen.incoming_delete_skipped", error=exc.message)
        except Exception:
            log.exception("screen.incoming_delete_failed")
