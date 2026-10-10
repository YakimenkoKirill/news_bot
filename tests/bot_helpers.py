"""Помощники для тестов бота: фейковая сессия Bot API и апдейты."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
TEST_TOKEN = "123456:TEST-TOKEN"


class FakeSession(BaseSession):
    """Сессия Bot API без сети: запоминает запросы и отвечает правдоподобно."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 сигнатура BaseSession
    ) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=NOW,
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 сигнатура BaseSession
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError
        yield b""

    def sent_texts(self) -> list[str]:
        """Тексты отправленных сообщений."""
        return [m.text for m in self.requests if isinstance(m, SendMessage)]

    def sent(self) -> list[SendMessage]:
        """Отправленные сообщения."""
        return [m for m in self.requests if isinstance(m, SendMessage)]


def make_bot() -> tuple[Bot, FakeSession]:
    """Бот с фейковой сессией."""
    session = FakeSession()
    return Bot(token=TEST_TOKEN, session=session), session


def user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name=f"user{user_id}")


def message_update(user_id: int, text: str, update_id: int = 1) -> Update:
    """Апдейт с личным сообщением от пользователя."""
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=NOW,
            chat=Chat(id=user_id, type="private"),
            from_user=user(user_id),
            text=text,
        ),
    )


def callback_update(user_id: int, data: str, update_id: int = 1) -> Update:
    """Апдейт с нажатием inline-кнопки."""
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=user(user_id),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=update_id,
                date=NOW,
                chat=Chat(id=user_id, type="private"),
                text="исходное сообщение",
            ),
        ),
    )
