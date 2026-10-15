import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import (
    DeleteMessage,
    SendAnimation,
    SendDocument,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SendVideo,
)
from aiogram.methods.base import Response
from aiogram.types import Chat, Message

from app.bot.screen import (
    ScreenRequestMiddleware,
    ScreenTracker,
    current_screen,
    new_screen,
    sent_message_ids,
)
from app.config import UiConfig
from tests.bot_helpers import make_bot
from tests.config_helpers import base_section

CHAT = 111
OTHER_CHAT = 222
CHANNEL = -1001234567890
GROUP = -100500
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
CFG = UiConfig(**base_section("ui"))
METHOD = SendMessage(chat_id=CHAT, text="x")


def _message(message_id: int, chat_id: int = CHAT) -> Message:
    return Message(message_id=message_id, date=NOW, chat=Chat(id=chat_id, type="private"), text="t")


def _bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=METHOD, message=text)


@dataclass
class Db:
    screens: dict[int, tuple[str, list[int]]] = field(default_factory=dict)
    fail_register: bool = False


class Repo:
    """Репозиторий экранов в памяти с той же логикой, что в БД."""

    def __init__(self, db: Db) -> None:
        self.db = db

    async def register(
        self, chat_id: int, token: str, message_ids: Sequence[int], at: datetime
    ) -> list[int]:
        if self.db.fail_register:
            raise RuntimeError("БД недоступна")
        current_token, current_ids = self.db.screens.get(chat_id, (token, []))
        if current_token == token:
            kept = list(current_ids)
            kept.extend(i for i in message_ids if i not in kept)
            obsolete: list[int] = []
        else:
            kept, obsolete = list(message_ids), list(current_ids)
        self.db.screens[chat_id] = (token, kept)
        return obsolete

    async def forget(self, chat_id: int, message_ids: Sequence[int]) -> None:
        token, ids = self.db.screens.get(chat_id, ("", []))
        self.db.screens[chat_id] = (token, [i for i in ids if i not in set(message_ids)])


class FakeBot:
    """Бот с delete_messages/delete_message и настраиваемыми ошибками."""

    def __init__(
        self,
        batch_error: Exception | None = None,
        single_errors: dict[int, Exception] | None = None,
    ) -> None:
        self.batch_error = batch_error
        self.single_errors = single_errors or {}
        self.batches: list[list[int]] = []
        self.singles: list[int] = []

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> None:
        self.batches.append(list(message_ids))
        if self.batch_error:
            raise self.batch_error

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.singles.append(message_id)
        if message_id in self.single_errors:
            raise self.single_errors[message_id]


def _tracker(db: Db, bot: FakeBot, cfg: UiConfig = CFG) -> ScreenTracker:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    return ScreenTracker(scope, Repo, bot, cfg, lambda: NOW)


def test_new_screen_sets_and_restores_token() -> None:
    outer = current_screen()
    with new_screen() as token:
        assert current_screen() == token
        with new_screen() as inner:
            assert inner != token
            assert current_screen() == inner
        assert current_screen() == token
    assert current_screen() != token
    assert outer != current_screen(), "вне экрана каждый вызов даёт новый номер"


def test_screen_tokens_are_unique() -> None:
    with new_screen() as first:
        pass
    with new_screen() as second:
        pass
    assert first != second


def test_sent_message_ids_handles_single_and_album() -> None:
    assert sent_message_ids(_message(5)) == [5]
    assert sent_message_ids([_message(1), _message(2)]) == [1, 2]
    assert sent_message_ids(True) == []
    assert sent_message_ids(None) == []


async def test_first_message_deletes_nothing() -> None:
    db, bot = Db(), FakeBot()
    await _tracker(db, bot).on_sent(CHAT, [10], "s1")
    assert bot.batches == []
    assert db.screens[CHAT] == ("s1", [10])


async def test_same_screen_accumulates() -> None:
    db, bot = Db(), FakeBot()
    tracker = _tracker(db, bot)
    await tracker.on_sent(CHAT, [10], "s1")
    await tracker.on_sent(CHAT, [11], "s1")
    assert bot.batches == []
    assert db.screens[CHAT] == ("s1", [10, 11])


async def test_new_screen_deletes_previous() -> None:
    db, bot = Db(), FakeBot()
    tracker = _tracker(db, bot)
    await tracker.on_sent(CHAT, [10, 11], "s1")
    await tracker.on_sent(CHAT, [20], "s2")
    assert bot.batches == [[10, 11]]
    assert db.screens[CHAT] == ("s2", [20])


async def test_album_messages_kept_together() -> None:
    db, bot = Db(), FakeBot()
    await _tracker(db, bot).on_sent(CHAT, [10, 11, 12], "s1")
    assert db.screens[CHAT] == ("s1", [10, 11, 12])


async def test_large_screen_deleted_in_batches() -> None:
    db, bot = Db(), FakeBot()
    cfg = CFG.model_copy(update={"delete_batch_size": 2})
    tracker = _tracker(db, bot, cfg)
    await tracker.on_sent(CHAT, [1, 2, 3, 4, 5], "s1")
    await tracker.on_sent(CHAT, [9], "s2")
    assert bot.batches == [[1, 2], [3, 4], [5]]


async def test_batch_failure_falls_back_to_one_by_one() -> None:
    db = Db()
    bot = FakeBot(batch_error=_bad_request("message to delete not found"))
    tracker = _tracker(db, bot)
    await tracker.on_sent(CHAT, [10, 11], "s1")
    await tracker.on_sent(CHAT, [20], "s2")
    assert bot.singles == [10, 11]


async def test_old_message_skipped_without_error() -> None:
    db = Db()
    bot = FakeBot(
        batch_error=_bad_request("message can't be deleted"),
        single_errors={10: _bad_request("message can't be deleted for everyone")},
    )
    tracker = _tracker(db, bot)
    with structlog.testing.capture_logs() as logs:
        await tracker.on_sent(CHAT, [10, 11], "s1")
        await tracker.on_sent(CHAT, [20], "s2")
    assert [e["event"] for e in logs if e["event"] == "screen.delete_skipped"] == [
        "screen.delete_skipped"
    ]
    assert db.screens[CHAT] == ("s2", [20]), "новое сообщение зарегистрировано"


async def test_unexpected_delete_error_logged_and_swallowed() -> None:
    db = Db()
    bot = FakeBot(batch_error=TelegramNetworkError(method=METHOD, message="network down"))
    tracker = _tracker(db, bot)
    await tracker.on_sent(CHAT, [10], "s1")
    with structlog.testing.capture_logs() as logs:
        await tracker.on_sent(CHAT, [20], "s2")
    assert any(e["event"] == "screen.delete_failed" for e in logs)
    assert db.screens[CHAT] == ("s2", [20])


async def test_register_failure_does_not_raise() -> None:
    db, bot = Db(fail_register=True), FakeBot()
    with structlog.testing.capture_logs() as logs:
        await _tracker(db, bot).on_sent(CHAT, [10], "s1")
    assert any(e["event"] == "screen.register_failed" for e in logs)
    assert bot.batches == []


async def test_empty_message_ids_ignored() -> None:
    db, bot = Db(), FakeBot()
    await _tracker(db, bot).on_sent(CHAT, [], "s1")
    assert db.screens == {}


async def test_chats_are_independent() -> None:
    db, bot = Db(), FakeBot()
    tracker = _tracker(db, bot)
    await tracker.on_sent(CHAT, [10], "s1")
    await tracker.on_sent(OTHER_CHAT, [20], "s1")
    await tracker.on_sent(CHAT, [11], "s2")
    assert bot.batches == [[10]]
    assert db.screens[OTHER_CHAT] == ("s1", [20])


async def test_parallel_sends_to_one_chat_are_serialised() -> None:
    db, bot = Db(), FakeBot()
    tracker = _tracker(db, bot)
    await asyncio.gather(
        tracker.on_sent(CHAT, [10], "s1"),
        tracker.on_sent(CHAT, [20], "s2"),
    )
    token, ids = db.screens[CHAT]
    deleted = [i for batch in bot.batches for i in batch]
    assert set(ids) | set(deleted) == {10, 20}
    assert set(ids) & set(deleted) == set()
    assert token in {"s1", "s2"}


class Sender:
    """Подменяет make_request: отдаёт заготовленный ответ."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    async def __call__(self, bot: Any, method: Any) -> Response[Any]:
        self.calls += 1
        return Response(ok=True, result=self.result)


def _middleware(db: Db, bot: FakeBot, cfg: UiConfig = CFG) -> ScreenRequestMiddleware:
    return ScreenRequestMiddleware(_tracker(db, bot), CHANNEL, cfg)


@pytest.mark.parametrize(
    "method",
    [
        SendMessage(chat_id=CHAT, text="t"),
        SendPhoto(chat_id=CHAT, photo="p"),
        SendDocument(chat_id=CHAT, document="d"),
        SendAnimation(chat_id=CHAT, animation="a"),
        SendVideo(chat_id=CHAT, video="v"),
    ],
)
async def test_every_send_method_registers(method: Any) -> None:
    db, bot = Db(), FakeBot()
    aiobot, _ = make_bot()
    sender = Sender(_message(10))
    response = await _middleware(db, bot)(sender, aiobot, method)
    assert response.result is not None
    assert db.screens[CHAT] == (db.screens[CHAT][0], [10])
    await aiobot.session.close()


async def test_media_group_registers_all() -> None:
    db, bot = Db(), FakeBot()
    aiobot, _ = make_bot()
    sender = Sender([_message(10), _message(11)])
    method = SendMediaGroup(chat_id=CHAT, media=[])
    await _middleware(db, bot)(sender, aiobot, method)
    assert db.screens[CHAT][1] == [10, 11]
    await aiobot.session.close()


@pytest.mark.parametrize("chat_id", [CHANNEL, GROUP])
async def test_channel_and_groups_untouched(chat_id: int) -> None:
    db, bot = Db(), FakeBot()
    aiobot, _ = make_bot()
    sender = Sender(_message(10, chat_id))
    await _middleware(db, bot)(sender, aiobot, SendMessage(chat_id=chat_id, text="t"))
    assert db.screens == {}
    assert bot.batches == []
    await aiobot.session.close()


async def test_other_methods_ignored() -> None:
    db, bot = Db(), FakeBot()
    aiobot, _ = make_bot()
    sender = Sender(True)
    await _middleware(db, bot)(sender, aiobot, DeleteMessage(chat_id=CHAT, message_id=1))
    assert db.screens == {}
    await aiobot.session.close()


async def test_disabled_single_screen_does_nothing() -> None:
    db, bot = Db(), FakeBot()
    aiobot, _ = make_bot()
    cfg = CFG.model_copy(update={"single_screen": False})
    sender = Sender(_message(10))
    await _middleware(db, bot, cfg)(sender, aiobot, SendMessage(chat_id=CHAT, text="t"))
    assert db.screens == {}
    assert sender.calls == 1
    await aiobot.session.close()


async def test_middleware_returns_response_unchanged() -> None:
    db, bot = Db(), FakeBot()
    aiobot, _ = make_bot()
    message = _message(10)
    response = await _middleware(db, bot)(
        Sender(message), aiobot, SendMessage(chat_id=CHAT, text="t")
    )
    assert response.result is message
    await aiobot.session.close()


async def test_tracker_failure_does_not_break_send() -> None:
    db, bot = Db(fail_register=True), FakeBot()
    aiobot, _ = make_bot()
    message = _message(10)
    response = await _middleware(db, bot)(
        Sender(message), aiobot, SendMessage(chat_id=CHAT, text="t")
    )
    assert response.result is message
    await aiobot.session.close()


async def test_messages_of_one_screen_survive_through_middleware() -> None:
    db, bot = Db(), FakeBot()
    aiobot, _ = make_bot()
    middleware = _middleware(db, bot)
    with new_screen():
        await middleware(Sender(_message(10)), aiobot, SendMessage(chat_id=CHAT, text="1"))
        await middleware(Sender(_message(11)), aiobot, SendMessage(chat_id=CHAT, text="2"))
    assert db.screens[CHAT][1] == [10, 11]
    assert bot.batches == []

    with new_screen():
        await middleware(Sender(_message(20)), aiobot, SendMessage(chat_id=CHAT, text="3"))
    assert bot.batches == [[10, 11]]
    await aiobot.session.close()


async def test_send_without_screen_is_its_own_screen() -> None:
    db, bot = Db(), FakeBot()
    aiobot, _ = make_bot()
    middleware = _middleware(db, bot)
    await middleware(Sender(_message(10)), aiobot, SendMessage(chat_id=CHAT, text="1"))
    await middleware(Sender(_message(11)), aiobot, SendMessage(chat_id=CHAT, text="2"))
    assert bot.batches == [[10]], "вторая отправка — новый экран"
    await aiobot.session.close()
