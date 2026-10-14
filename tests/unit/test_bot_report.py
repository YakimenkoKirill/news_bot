from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import InlineKeyboardMarkup, Update

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import report as report_handlers
from app.bot.keyboards import ReportAction, report_keyboard
from app.config import DailyReportConfig, MetricsConfig, Settings
from app.reports.daily import DailyContext
from app.reports.service import ReportDeps, ReportPeriod
from tests.bot_helpers import FakeSession, callback_update, make_bot, message_update
from tests.config_helpers import base_section, bot_config

ADMIN = 111
MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=MSK)
CFG = DailyReportConfig(**base_section("daily_report"))
METRICS = MetricsConfig(**base_section("metrics"))
BUDGET = Decimal("0.5")
LIMIT = 4096
CTX = DailyContext(tz=MSK, daily_budget=BUDGET, decay_per_h=0.02, top_posts=3, channel_id=-100)
CALLBACK_DATA_LIMIT = 64


class Builder:
    """Подменяет build_report: отдаёт заготовленные сообщения или падает."""

    def __init__(self, messages: list[str] | None = None, error: Exception | None = None) -> None:
        self.messages = messages if messages is not None else ["отчёт"]
        self.error = error
        self.calls: list[ReportPeriod] = []
        self.requests_before: list[Any] = []
        self.session: FakeSession | None = None

    async def __call__(self, kind: ReportPeriod, deps: ReportDeps) -> list[str]:
        self.calls.append(kind)
        if self.session is not None:
            self.requests_before = list(self.session.requests)
        if self.error:
            raise self.error
        return self.messages


def _deps() -> ReportDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[object]:
        yield object()

    async def is_paused(session: Any) -> bool:
        return False

    return ReportDeps(
        session_scope=scope,
        context=lambda: CTX,
        cfg=CFG,
        metrics=METRICS,
        text_limit=LIMIT,
        budget=BUDGET,
        tz=MSK,
        clock=lambda: NOW,
        paused=is_paused,
    )


async def _feed(update: Update, builder: Builder, monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    class NoAdmins:
        async def is_admin(self, telegram_id: int) -> bool:
            return False

    monkeypatch.setattr(report_handlers, "build_report", builder)
    deps = BotDeps(
        settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
        admin_repo=NoAdmins(),
        routers=[report_handlers.create_router()],
        workflow_data={"report_deps": _deps()},
    )
    bot, session = make_bot()
    builder.session = session
    await create_dispatcher(deps).feed_update(bot, update)
    return session


def _cb(period: str) -> Update:
    return callback_update(ADMIN, ReportAction(period=period).pack())


def _sent(session: FakeSession) -> list[SendMessage]:
    return [r for r in session.requests if isinstance(r, SendMessage)]


async def test_command_sends_today_report(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = Builder(["📊 Отчёт"])
    session = await _feed(message_update(ADMIN, "/report"), builder, monkeypatch)
    assert builder.calls == [ReportPeriod.TODAY]
    [sent] = _sent(session)
    assert sent.text == "📊 Отчёт"
    assert sent.reply_markup == report_keyboard("today")


@pytest.mark.parametrize("period", ["today", "yesterday", "week"])
async def test_each_period_button(period: str, monkeypatch: pytest.MonkeyPatch) -> None:
    builder = Builder()
    session = await _feed(_cb(period), builder, monkeypatch)
    assert builder.calls == [ReportPeriod(period)]
    [sent] = _sent(session)
    assert sent.reply_markup == report_keyboard(period)


async def test_callback_answered_before_building(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = Builder()
    await _feed(_cb("week"), builder, monkeypatch)
    answered = [r for r in builder.requests_before if isinstance(r, AnswerCallbackQuery)]
    assert [a.text for a in answered] == [texts.REPORT_BUILDING]


async def test_keyboard_only_under_last_message(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = Builder(["часть 1", "часть 2", "часть 3"])
    session = await _feed(message_update(ADMIN, "/report"), builder, monkeypatch)
    sent = _sent(session)
    assert [m.text for m in sent] == ["часть 1", "часть 2", "часть 3"]
    assert [m.reply_markup for m in sent[:-1]] == [None, None]
    assert isinstance(sent[-1].reply_markup, InlineKeyboardMarkup)


async def test_build_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = Builder(error=RuntimeError("БД недоступна"))
    with structlog.testing.capture_logs() as logs:
        session = await _feed(message_update(ADMIN, "/report"), builder, monkeypatch)
    assert [m.text for m in _sent(session)] == [texts.REPORT_FAILED]
    assert [entry["period"] for entry in logs if entry["event"] == "report.failed"] == ["today"]


async def test_build_failure_on_callback_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = Builder(error=RuntimeError("БД недоступна"))
    session = await _feed(_cb("yesterday"), builder, monkeypatch)
    answers = [r for r in session.requests if isinstance(r, AnswerCallbackQuery)]
    assert [a.text for a in answers] == [texts.REPORT_BUILDING]
    assert [m.text for m in _sent(session)] == [texts.REPORT_FAILED]


async def test_double_press_gives_two_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = Builder()
    first = await _feed(_cb("today"), builder, monkeypatch)
    second = await _feed(_cb("today"), builder, monkeypatch)
    assert builder.calls == [ReportPeriod.TODAY, ReportPeriod.TODAY]
    assert len(_sent(first)) == 1
    assert len(_sent(second)) == 1


def test_keyboard_marks_current_period() -> None:
    labels = [b.text for row in report_keyboard("week").inline_keyboard for b in row]
    assert labels == ["Сегодня", "Вчера", "• 7 дней"]


def test_callback_data_fits_limit() -> None:
    for period in ReportPeriod:
        data = ReportAction(period=period.value).pack()
        assert len(data.encode()) <= CALLBACK_DATA_LIMIT
