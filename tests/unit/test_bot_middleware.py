from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from aiogram.methods import AnswerCallbackQuery, DeleteMessage
from aiogram.types import Update

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import errors as errors_handlers
from app.bot.handlers import queue as queue_handlers
from app.bot.handlers import report as report_handlers
from app.bot.keyboards import (
    ErrorAction,
    ErrorActionType,
    ErrorTab,
    QueueAction,
    QueueActionType,
    QueueTab,
    ReportAction,
)
from app.bot.middlewares import AdminOnlyMiddleware
from app.bot.screen import ScreenUpdateMiddleware
from app.config import Settings, UiConfig
from app.logging import setup_logging
from tests.bot_helpers import FakeSession, callback_update, make_bot, message_update
from tests.config_helpers import base_section, bot_config
from tests.unit.test_bot_queue import Db, _deps
from tests.unit.test_bot_report import Builder
from tests.unit.test_bot_report import _deps as report_deps

SETTINGS_ADMIN = 111
DB_ADMIN = 222
STRANGER = 333
COOLDOWN = 60.0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class AdminRepo:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def is_admin(self, telegram_id: int) -> bool:
        self.calls.append(telegram_id)
        return telegram_id == DB_ADMIN


class Event(SimpleNamespace):
    """Фейковое сообщение (или callback, если передан chat_instance)."""

    def __init__(self, user_id: int | None, text: str = "секретный текст", **extra: Any) -> None:
        user = SimpleNamespace(id=user_id) if user_id is not None else None
        super().__init__(from_user=user, text=text, answers=[], **extra)

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


def _callback(user_id: int) -> Event:
    return Event(user_id, chat_instance="ci", data="review:1")


class Handler:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event: Any, data: dict[str, Any]) -> str:
        self.events.append(event)
        return "handled"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repo() -> AdminRepo:
    return AdminRepo()


@pytest.fixture
def middleware(clock: Clock, repo: AdminRepo) -> AdminOnlyMiddleware:
    settings = Settings.model_construct(
        telegram_admin_ids=[SETTINGS_ADMIN], bot=bot_config(refusal_cooldown_s=COOLDOWN)
    )
    return AdminOnlyMiddleware(settings, repo, clock=clock)


async def test_admin_from_settings_passes(middleware: AdminOnlyMiddleware, repo: AdminRepo) -> None:
    handler, event = Handler(), Event(SETTINGS_ADMIN)
    assert await middleware(handler, event, {}) == "handled"  # type: ignore[arg-type]
    assert handler.events == [event]
    assert repo.calls == []


async def test_admin_from_db_passes(middleware: AdminOnlyMiddleware) -> None:
    handler = Handler()
    assert await middleware(handler, Event(DB_ADMIN), {}) == "handled"  # type: ignore[arg-type]


async def test_stranger_refused_and_logged_without_text(
    middleware: AdminOnlyMiddleware, capsys: pytest.CaptureFixture[str]
) -> None:
    handler, event = Handler(), Event(STRANGER)
    setup_logging("INFO", json_output=True)
    try:
        assert await middleware(handler, event, {}) is None  # type: ignore[arg-type]
    finally:
        structlog.reset_defaults()
    assert handler.events == []
    assert event.answers == [(texts.ACCESS_DENIED, {})]
    out = capsys.readouterr().out
    assert '"user_id": 333' in out
    assert "секретный текст" not in out


async def test_repeated_refusal_within_cooldown(
    middleware: AdminOnlyMiddleware, clock: Clock
) -> None:
    first, second, third = Event(STRANGER), Event(STRANGER), Event(STRANGER)
    await middleware(Handler(), first, {})  # type: ignore[arg-type]
    clock.now += COOLDOWN - 1
    await middleware(Handler(), second, {})  # type: ignore[arg-type]
    clock.now += 1
    await middleware(Handler(), third, {})  # type: ignore[arg-type]
    assert len(first.answers) == 1
    assert second.answers == []
    assert len(third.answers) == 1


async def test_update_without_user_dropped_silently(middleware: AdminOnlyMiddleware) -> None:
    handler, event = Handler(), Event(None)
    assert await middleware(handler, event, {}) is None  # type: ignore[arg-type]
    assert handler.events == []
    assert event.answers == []


async def test_callback_from_stranger_answered(
    middleware: AdminOnlyMiddleware, clock: Clock
) -> None:
    first, second = _callback(STRANGER), _callback(STRANGER)
    await middleware(Handler(), first, {})  # type: ignore[arg-type]
    await middleware(Handler(), second, {})  # type: ignore[arg-type]
    assert first.answers == [(texts.ACCESS_DENIED, {"show_alert": True})]
    assert second.answers == [(None, {"show_alert": False})]


async def test_callback_from_admin_passes(middleware: AdminOnlyMiddleware) -> None:
    handler, event = Handler(), _callback(SETTINGS_ADMIN)
    assert await middleware(handler, event, {}) == "handled"  # type: ignore[arg-type]
    assert event.answers == []


async def _feed_queue(update: Update) -> FakeSession:
    deps = BotDeps(
        settings=Settings.model_construct(
            telegram_admin_ids=[SETTINGS_ADMIN], bot=bot_config(refusal_cooldown_s=COOLDOWN)
        ),
        admin_repo=AdminRepo(),
        routers=[queue_handlers.create_router()],
        workflow_data={"queue_deps": _deps(Db())},
    )
    bot, session = make_bot()
    await create_dispatcher(deps).feed_update(bot, update)
    return session


def _queue_callback(user_id: int) -> Update:
    data = QueueAction(action=QueueActionType.CONFIRM, tab=QueueTab.QUEUE, page=0, news_id=1)
    return callback_update(user_id, data.pack())


async def test_stranger_refused_on_queue_command() -> None:
    session = await _feed_queue(message_update(STRANGER, "/queue"))
    assert session.sent_texts() == [texts.ACCESS_DENIED]


async def test_stranger_refused_on_queue_callback() -> None:
    session = await _feed_queue(_queue_callback(STRANGER))
    answers = [r for r in session.requests if isinstance(r, AnswerCallbackQuery)]
    assert [(a.text, a.show_alert) for a in answers] == [(texts.ACCESS_DENIED, True)]
    assert not [r for r in session.requests if not isinstance(r, AnswerCallbackQuery)]


async def test_admin_queue_not_caught_by_fallback() -> None:
    session = await _feed_queue(message_update(SETTINGS_ADMIN, "/queue"))
    [text] = session.sent_texts()
    assert text.startswith("<b>Очередь публикации</b>")
    assert text != texts.UNKNOWN_COMMAND


async def _feed_errors(update: Update) -> FakeSession:
    from tests.unit.test_bot_errors import Db as ErrorsDb
    from tests.unit.test_bot_errors import _deps as errors_deps

    deps = BotDeps(
        settings=Settings.model_construct(
            telegram_admin_ids=[SETTINGS_ADMIN], bot=bot_config(refusal_cooldown_s=COOLDOWN)
        ),
        admin_repo=AdminRepo(),
        routers=[errors_handlers.create_router()],
        workflow_data={"errors_deps": errors_deps(ErrorsDb())},
    )
    bot, session = make_bot()
    await create_dispatcher(deps).feed_update(bot, update)
    return session


def _errors_callback(user_id: int) -> Update:
    data = ErrorAction(action=ErrorActionType.RESOLVE, tab=ErrorTab.OPEN, page=0, id=1)
    return callback_update(user_id, data.pack())


async def test_stranger_refused_on_errors_command() -> None:
    session = await _feed_errors(message_update(STRANGER, "/errors"))
    assert session.sent_texts() == [texts.ACCESS_DENIED]


async def test_stranger_refused_on_errors_callback() -> None:
    session = await _feed_errors(_errors_callback(STRANGER))
    answers = [r for r in session.requests if isinstance(r, AnswerCallbackQuery)]
    assert [(a.text, a.show_alert) for a in answers] == [(texts.ACCESS_DENIED, True)]
    assert not [r for r in session.requests if not isinstance(r, AnswerCallbackQuery)]


async def test_admin_errors_command_passes() -> None:
    session = await _feed_errors(message_update(SETTINGS_ADMIN, "/errors"))
    [text] = session.sent_texts()
    assert text.startswith("<b>Ошибки</b>")


async def _feed_report(update: Update, monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    monkeypatch.setattr(report_handlers, "build_report", Builder(["отчёт"]))
    deps = BotDeps(
        settings=Settings.model_construct(
            telegram_admin_ids=[SETTINGS_ADMIN], bot=bot_config(refusal_cooldown_s=COOLDOWN)
        ),
        admin_repo=AdminRepo(),
        routers=[report_handlers.create_router()],
        workflow_data={"report_deps": report_deps()},
    )
    bot, session = make_bot()
    await create_dispatcher(deps).feed_update(bot, update)
    return session


async def test_stranger_refused_on_report_command(monkeypatch: pytest.MonkeyPatch) -> None:
    session = await _feed_report(message_update(STRANGER, "/report"), monkeypatch)
    assert session.sent_texts() == [texts.ACCESS_DENIED]


async def test_stranger_refused_on_report_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    update = callback_update(STRANGER, ReportAction(period="week").pack())
    session = await _feed_report(update, monkeypatch)
    answers = [r for r in session.requests if isinstance(r, AnswerCallbackQuery)]
    assert [(a.text, a.show_alert) for a in answers] == [(texts.ACCESS_DENIED, True)]
    assert not [r for r in session.requests if not isinstance(r, AnswerCallbackQuery)]


def _dispatcher_with_screens() -> Any:
    return create_dispatcher(
        BotDeps(
            settings=Settings.model_construct(
                telegram_admin_ids=[SETTINGS_ADMIN], bot=bot_config(refusal_cooldown_s=COOLDOWN)
            ),
            admin_repo=AdminRepo(),
            screen_middleware=ScreenUpdateMiddleware(UiConfig(**base_section("ui"))),
        )
    )


def test_access_check_runs_before_screens() -> None:
    """Постороннему экран не создаётся и его сообщение не удаляется."""
    dispatcher = _dispatcher_with_screens()
    for observer in (dispatcher.message, dispatcher.callback_query):
        kinds = [type(m) for m in observer.outer_middleware]
        assert kinds.index(AdminOnlyMiddleware) < kinds.index(ScreenUpdateMiddleware)


def test_screens_not_added_without_middleware() -> None:
    dispatcher = create_dispatcher(
        BotDeps(
            settings=Settings.model_construct(
                telegram_admin_ids=[SETTINGS_ADMIN], bot=bot_config()
            ),
            admin_repo=AdminRepo(),
        )
    )
    for observer in (dispatcher.message, dispatcher.callback_query):
        assert not any(isinstance(m, ScreenUpdateMiddleware) for m in observer.outer_middleware)


async def test_stranger_message_not_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    bot, session = make_bot()
    await _dispatcher_with_screens().feed_update(bot, message_update(STRANGER, "/status"))
    assert session.sent_texts() == [texts.ACCESS_DENIED]
    assert not [r for r in session.requests if isinstance(r, DeleteMessage)]
