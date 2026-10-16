from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.bot.screen import current_screen
from app.config import DailyReportConfig, MetricsConfig
from app.reports import sender as sender_module
from app.reports.daily import DailyContext
from app.reports.sender import DAILY_REPORT_SETTING, DailyReportSender
from app.reports.service import ReportDeps, ReportPeriod
from tests.config_helpers import base_section

MSK = ZoneInfo("Europe/Moscow")
ADMINS = [111, 222]
CFG = DailyReportConfig(**base_section("daily_report"))
CURRENT_CFG = CFG.model_copy(update={"report_day": "current", "send_at": "23:30"})
METRICS = MetricsConfig(**base_section("metrics"))
BUDGET = Decimal("0.5")
CTX = DailyContext(tz=MSK, daily_budget=BUDGET, decay_per_h=0.02, top_posts=3, channel_id=-100)


def msk(hour: int, minute: int = 0, day: int = 14) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=MSK)


class Db:
    def __init__(self, last: str | None = None) -> None:
        self.settings: dict[str, Any] = {DAILY_REPORT_SETTING: last} if last else {}
        self.locks = 0


class Settings:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get_for_update(self, key: str, default: Any = None) -> Any:
        self.db.locks += 1
        return self.db.settings.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self.db.settings[key] = value


class Bot:
    def __init__(self, failing: set[int] | None = None) -> None:
        self.failing = failing or set()
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        if kwargs["chat_id"] in self.failing:
            raise RuntimeError("bot was blocked")
        self.sent.append(kwargs)


def _deps(db: Db, now: datetime) -> ReportDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    async def is_paused(session: Any) -> bool:
        return False

    return ReportDeps(
        session_scope=scope,
        context=lambda: CTX,
        cfg=CFG,
        metrics=METRICS,
        text_limit=4096,
        budget=BUDGET,
        tz=MSK,
        clock=lambda: now,
        paused=is_paused,
    )


def _sender(
    db: Db,
    now: datetime,
    bot: Bot | None = None,
    cfg: DailyReportConfig = CFG,
    admins: Any = ADMINS,
) -> tuple[DailyReportSender, Bot]:
    bot = bot or Bot()
    return DailyReportSender(bot, admins, Settings, _deps(db, now), cfg), bot


@pytest.fixture(autouse=True)
def fake_build(monkeypatch: pytest.MonkeyPatch) -> list[ReportPeriod]:
    calls: list[ReportPeriod] = []

    async def build(kind: ReportPeriod, deps: ReportDeps) -> list[str]:
        calls.append(kind)
        return ["часть 1", "часть 2"]

    monkeypatch.setattr(sender_module, "build_report", build)
    return calls


async def test_sends_report_for_previous_day(fake_build: list[ReportPeriod]) -> None:
    db = Db()
    sender, bot = _sender(db, msk(0, 5, day=15))

    assert await sender.send_due(msk(0, 5, day=15)) is True
    assert fake_build == [ReportPeriod.YESTERDAY]
    assert [m["chat_id"] for m in bot.sent] == [111, 111, 222, 222]
    assert db.settings[DAILY_REPORT_SETTING] == "2026-10-14"


async def test_sends_report_for_current_day(fake_build: list[ReportPeriod]) -> None:
    db = Db()
    sender, bot = _sender(db, msk(23, 30), cfg=CURRENT_CFG)

    assert await sender.send_due(msk(23, 30)) is True
    assert fake_build == [ReportPeriod.TODAY]
    assert db.settings[DAILY_REPORT_SETTING] == "2026-10-14"
    assert bot.sent


async def test_keyboard_marks_report_period() -> None:
    sender, bot = _sender(Db(), msk(0, 5, day=15))
    await sender.send_due(msk(0, 5, day=15))

    keyboard = bot.sent[-1]["reply_markup"]
    marked = [b.text for row in keyboard.inline_keyboard for b in row if b.text.startswith("• ")]
    assert marked == ["• Вчера"]


async def test_keyboard_marks_today_for_current_report_day() -> None:
    sender, bot = _sender(Db(), msk(23, 30), cfg=CURRENT_CFG)
    await sender.send_due(msk(23, 30))

    keyboard = bot.sent[-1]["reply_markup"]
    marked = [b.text for row in keyboard.inline_keyboard for b in row if b.text.startswith("• ")]
    assert marked == ["• Сегодня"]


async def test_keyboard_only_under_last_message() -> None:
    sender, bot = _sender(Db(), msk(0, 5, day=15))
    await sender.send_due(msk(0, 5, day=15))
    for admin in ADMINS:
        messages = [m for m in bot.sent if m["chat_id"] == admin]
        assert messages[0]["reply_markup"] is None
        assert messages[-1]["reply_markup"] is not None


async def test_not_sent_before_send_at() -> None:
    sender, bot = _sender(Db(), msk(0, 4, day=15))
    assert await sender.send_due(msk(0, 4, day=15)) is False
    assert bot.sent == []


async def test_not_sent_twice_for_one_report_day() -> None:
    db = Db()
    sender, _ = _sender(db, msk(0, 5, day=15))
    assert await sender.send_due(msk(0, 5, day=15)) is True

    later, later_bot = _sender(db, msk(0, 20, day=15))
    assert await later.send_due(msk(0, 20, day=15)) is False
    assert later_bot.sent == []


async def test_not_resent_before_midnight_of_next_day() -> None:
    """Отчёт за 14-е ушёл в 00:05 15-го; вечером 15-го он не повторяется."""
    db = Db(last="2026-10-14")
    sender, bot = _sender(db, msk(23, 50, day=15))
    assert await sender.send_due(msk(23, 50, day=15)) is False
    assert bot.sent == []


async def test_sent_after_restart_when_missed(fake_build: list[ReportPeriod]) -> None:
    """Приложение не работало с 00:00 до 09:00 — отчёт за вчера уходит при запуске задачи."""
    db = Db(last="2026-10-13")
    sender, bot = _sender(db, msk(9, 0, day=15))
    assert await sender.send_due(msk(9, 0, day=15)) is True
    assert fake_build == [ReportPeriod.YESTERDAY]
    assert db.settings[DAILY_REPORT_SETTING] == "2026-10-14"
    assert bot.sent


async def test_old_setting_format_does_not_cause_duplicate() -> None:
    """В настройке дата отправки прежнего отчёта (23:30 14-го) — в 00:20 дубля нет."""
    db = Db(last="2026-10-14")
    sender, bot = _sender(db, msk(0, 20, day=15))
    assert await sender.send_due(msk(0, 20, day=15)) is False
    assert bot.sent == []
    assert db.settings[DAILY_REPORT_SETTING] == "2026-10-14"


async def test_month_and_year_boundary() -> None:
    db = Db()
    sender, _ = _sender(db, datetime(2027, 1, 1, 0, 5, tzinfo=MSK))
    assert await sender.send_due(datetime(2027, 1, 1, 0, 5, tzinfo=MSK)) is True
    assert db.settings[DAILY_REPORT_SETTING] == "2026-12-31"

    march = Db()
    sender, _ = _sender(march, datetime(2026, 3, 1, 0, 5, tzinfo=MSK))
    assert await sender.send_due(datetime(2026, 3, 1, 0, 5, tzinfo=MSK)) is True
    assert march.settings[DAILY_REPORT_SETTING] == "2026-02-28"


async def test_disabled_sends_nothing() -> None:
    cfg = CFG.model_copy(update={"enabled": False})
    db = Db()
    sender, bot = _sender(db, msk(0, 5, day=15), cfg=cfg)
    assert await sender.send_due(msk(0, 5, day=15)) is False
    assert bot.sent == []
    assert db.settings == {}


async def test_one_admin_failure_does_not_block_others() -> None:
    db = Db()
    sender, bot = _sender(db, msk(0, 5, day=15), bot=Bot(failing={111}))
    assert await sender.send_due(msk(0, 5, day=15)) is True
    assert {m["chat_id"] for m in bot.sent} == {222}
    assert db.settings[DAILY_REPORT_SETTING] == "2026-10-14"


async def test_date_not_written_when_nobody_received() -> None:
    db = Db()
    sender, bot = _sender(db, msk(0, 5, day=15), bot=Bot(failing=set(ADMINS)))
    assert await sender.send_due(msk(0, 5, day=15)) is False
    assert bot.sent == []
    assert DAILY_REPORT_SETTING not in db.settings

    retry, retry_bot = _sender(db, msk(0, 10, day=15))
    assert await retry.send_due(msk(0, 10, day=15)) is True
    assert retry_bot.sent


async def test_setting_is_read_with_lock() -> None:
    db = Db()
    sender, _ = _sender(db, msk(0, 5, day=15))
    await sender.send_due(msk(0, 5, day=15))
    assert db.locks == 1


async def test_admins_from_async_source() -> None:
    async def admins() -> Sequence[int]:
        return [7]

    sender, bot = _sender(Db(), msk(0, 5, day=15), admins=admins)
    assert await sender.send_due(msk(0, 5, day=15)) is True
    assert {m["chat_id"] for m in bot.sent} == {7}


async def test_day_boundary_uses_channel_timezone() -> None:
    """20:30 UTC — это ещё 23:30 того же дня по Москве, отчёт за сутки ещё не пора."""
    db = Db()
    sender, bot = _sender(db, msk(23, 30), cfg=CURRENT_CFG.model_copy(update={"send_at": "23:40"}))
    assert await sender.send_due(datetime(2026, 10, 14, 20, 30, tzinfo=UTC)) is False
    assert bot.sent == []


async def test_report_parts_share_screen_per_admin() -> None:
    """Все части отчёта одному админу — один экран, у разных админов экраны разные."""
    screens: list[tuple[int, str]] = []

    class ScreenBot(Bot):
        async def send_message(self, **kwargs: Any) -> None:
            screens.append((kwargs["chat_id"], current_screen()))
            await super().send_message(**kwargs)

    sender, _ = _sender(Db(), msk(23, 30), bot=ScreenBot())
    assert await sender.send_due(msk(23, 30)) is True

    by_admin: dict[int, set[str]] = {}
    for chat_id, token in screens:
        by_admin.setdefault(chat_id, set()).add(token)
    assert set(by_admin) == set(ADMINS)
    assert all(len(tokens) == 1 for tokens in by_admin.values())
    assert len({next(iter(t)) for t in by_admin.values()}) == len(ADMINS)
