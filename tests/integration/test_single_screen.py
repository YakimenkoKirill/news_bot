from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import DeleteMessage, DeleteMessages, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.alerts import AlertService
from app.bot.screen import ScreenRequestMiddleware, ScreenTracker, ScreenUpdateMiddleware
from app.config import AlertsConfig, PublisherConfig, UiConfig
from app.db.base import session_scope
from app.db.models import BotScreen
from app.db.repositories.screens import ScreenRepository
from app.domain.alerts import AlertEvent, AlertKind
from app.publisher.limits import FittedPost
from app.publisher.publisher import Publisher
from tests.bot_helpers import TEST_TOKEN
from tests.config_helpers import base_section
from tests.fakes import no_sleep

pytestmark = pytest.mark.integration

ADMIN = 111
OTHER_ADMIN = 222
CHANNEL = -1001234567890
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
MSK = ZoneInfo("Europe/Moscow")
UI = UiConfig(**base_section("ui"))
ALERTS = AlertsConfig(**base_section("alerts"))
PUBLISHER = PublisherConfig(**{**base_section("publisher"), "quiet_hours": []})


class FakeSession(BaseSession):
    """Сессия Bot API без сети: выдаёт растущие message_id и помнит удаления."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.next_id = 100

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 сигнатура BaseSession
    ) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage):
            self.next_id += 1
            return Message(
                message_id=self.next_id,
                date=NOW,
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
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

    def deleted(self) -> list[tuple[int, list[int]]]:
        """Все удаления: (chat_id, message_ids)."""
        result: list[tuple[int, list[int]]] = []
        for request in self.requests:
            if isinstance(request, DeleteMessages):
                result.append((int(request.chat_id), list(request.message_ids)))
            elif isinstance(request, DeleteMessage):
                result.append((int(request.chat_id), [request.message_id]))
        return result

    def sent_ids(self, chat_id: int) -> list[int]:
        ids: list[int] = []
        start = 100
        for request in self.requests:
            if isinstance(request, SendMessage):
                start += 1
                if int(request.chat_id) == chat_id:
                    ids.append(start)
        return ids


class Env:
    """Бот с экранами поверх реальной БД."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], ui: UiConfig = UI) -> None:
        self.factory = factory
        self.session = FakeSession()
        self.bot = Bot(token=TEST_TOKEN, session=self.session)
        self.bot.session.middleware(ScreenRequestMiddleware(self.tracker(ui), CHANNEL, ui))

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    def tracker(self, ui: UiConfig = UI) -> ScreenTracker:
        return ScreenTracker(self.scope, ScreenRepository, self.bot, ui, lambda: NOW)

    async def screen(self, chat_id: int) -> BotScreen | None:
        async with self.scope() as session:
            return await ScreenRepository(session).get(chat_id)


CALLBACK = CallbackQuery(
    id="1",
    from_user=User(id=ADMIN, is_bot=False, first_name="admin"),
    chat_instance="ci",
    data="x",
)


async def _handle(middleware: ScreenUpdateMiddleware, action: Any) -> None:
    """Выполняет действие так, как это делает диспетчер: внутри экрана.

    Событие — нажатие кнопки: входящее сообщение в этих сценариях не удаляется.
    """

    async def handler(event: Any, data: dict[str, Any]) -> None:
        await action()

    await middleware(handler, CALLBACK, {})


async def test_next_action_replaces_previous_screen(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    middleware = ScreenUpdateMiddleware(UI)

    await _handle(middleware, lambda: env.bot.send_message(chat_id=ADMIN, text="/queue"))
    first = env.session.sent_ids(ADMIN)
    assert len(first) == 1
    assert env.session.deleted() == []

    await _handle(middleware, lambda: env.bot.send_message(chat_id=ADMIN, text="карточка"))
    assert env.session.deleted() == [(ADMIN, first)]

    screen = await env.screen(ADMIN)
    assert screen is not None
    assert list(screen.message_ids) == env.session.sent_ids(ADMIN)[1:]


async def test_alert_to_two_admins_keeps_screens_apart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    service = AlertService(env.bot, [ADMIN, OTHER_ADMIN], ALERTS, clock=lambda: 0.0)

    await service.emit(
        AlertEvent(kind=AlertKind.AI_DOWN, key="ai_down:1", message="первый", created_at=NOW)
    )
    first = {chat: env.session.sent_ids(chat) for chat in (ADMIN, OTHER_ADMIN)}
    assert env.session.deleted() == []

    await service.emit(
        AlertEvent(
            kind=AlertKind.SOURCE_DOWN,
            key="source_down:1",
            message="второй",
            created_at=NOW,
        )
    )
    deleted = dict(env.session.deleted())
    assert deleted == {ADMIN: first[ADMIN], OTHER_ADMIN: first[OTHER_ADMIN]}

    for chat in (ADMIN, OTHER_ADMIN):
        screen = await env.screen(chat)
        assert screen is not None
        assert len(screen.message_ids) == 1


async def test_multipart_report_is_one_screen(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    middleware = ScreenUpdateMiddleware(UI)

    async def send_report() -> None:
        for part in ("часть 1", "часть 2", "часть 3"):
            await env.bot.send_message(chat_id=ADMIN, text=part)

    await _handle(middleware, send_report)
    parts = env.session.sent_ids(ADMIN)
    assert len(parts) == 3
    assert env.session.deleted() == []

    screen = await env.screen(ADMIN)
    assert screen is not None
    assert list(screen.message_ids) == parts

    await _handle(middleware, lambda: env.bot.send_message(chat_id=ADMIN, text="/status"))
    assert env.session.deleted() == [(ADMIN, parts)]


async def test_channel_is_never_touched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    publisher = Publisher(env.bot, CHANNEL, PUBLISHER, sleep=no_sleep)

    for text in ("<b>Первый пост</b>", "<b>Второй пост</b>"):
        await publisher.send(FittedPost(html=text, use_photo=False), None)

    assert await env.screen(CHANNEL) is None
    assert env.session.deleted() == [], "посты в канале не удаляются"


async def test_screen_survives_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    before = Env(session_factory)
    await _handle(
        ScreenUpdateMiddleware(UI),
        lambda: before.bot.send_message(chat_id=ADMIN, text="до перезапуска"),
    )
    old_ids = before.session.sent_ids(ADMIN)
    assert len(old_ids) == 1

    after = Env(session_factory)  # новый процесс, та же БД
    await _handle(
        ScreenUpdateMiddleware(UI),
        lambda: after.bot.send_message(chat_id=ADMIN, text="после перезапуска"),
    )
    assert after.session.deleted() == [(ADMIN, old_ids)]


async def test_disabled_single_screen_keeps_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    off = UI.model_copy(update={"single_screen": False})
    env = Env(session_factory, off)
    middleware = ScreenUpdateMiddleware(off)

    for text in ("первое", "второе"):
        await _handle(middleware, lambda text=text: env.bot.send_message(chat_id=ADMIN, text=text))

    assert env.session.deleted() == []
    assert await env.screen(ADMIN) is None, "записей об экранах не появляется"


async def test_first_message_has_nothing_to_delete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    await _handle(
        ScreenUpdateMiddleware(UI), lambda: env.bot.send_message(chat_id=ADMIN, text="привет")
    )
    assert env.session.deleted() == []
    screen = await env.screen(ADMIN)
    assert screen is not None
    assert len(screen.message_ids) == 1
