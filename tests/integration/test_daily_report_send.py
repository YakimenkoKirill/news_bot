import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import DailyReportConfig, MetricsConfig
from app.db.base import session_scope
from app.db.repositories.misc import SettingsRepository
from app.reports import sender as sender_module
from app.reports.daily import DailyContext
from app.reports.sender import DAILY_REPORT_SETTING, DailyReportSender
from app.reports.service import ReportDeps, ReportPeriod
from tests.config_helpers import base_section

pytestmark = pytest.mark.integration

MSK = ZoneInfo("Europe/Moscow")
ADMIN = 111
CFG = DailyReportConfig(**base_section("daily_report"))
METRICS = MetricsConfig(**base_section("metrics"))
BUDGET = Decimal("0.5")
NOW = datetime(2026, 10, 15, 0, 5, tzinfo=MSK)
CTX = DailyContext(tz=MSK, daily_budget=BUDGET, decay_per_h=0.02, top_posts=3, channel_id=-100)


class Bot:
    """Бот, который считает отправленные сообщения."""

    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_message(self, **kwargs: Any) -> None:
        await asyncio.sleep(0)
        self.sent.append(int(kwargs["chat_id"]))


def _deps(factory: async_sessionmaker[AsyncSession]) -> ReportDeps:
    def scope() -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(factory)

    async def is_paused(session: AsyncSession) -> bool:
        return False

    return ReportDeps(
        session_scope=scope,
        context=lambda: CTX,
        cfg=CFG,
        metrics=METRICS,
        text_limit=4096,
        budget=BUDGET,
        tz=MSK,
        clock=lambda: NOW,
        paused=is_paused,
    )


@pytest.fixture(autouse=True)
def fake_build(monkeypatch: pytest.MonkeyPatch) -> None:
    async def build(kind: ReportPeriod, deps: ReportDeps) -> list[str]:
        return ["отчёт"]

    monkeypatch.setattr(sender_module, "build_report", build)


async def test_two_instances_send_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Два экземпляра приложения: блокировка строки настройки даёт один отчёт."""
    first_bot, second_bot = Bot(), Bot()
    first = DailyReportSender(first_bot, [ADMIN], SettingsRepository, _deps(session_factory), CFG)
    second = DailyReportSender(second_bot, [ADMIN], SettingsRepository, _deps(session_factory), CFG)

    results = await asyncio.gather(first.send_due(NOW), second.send_due(NOW))

    assert sorted(results) == [False, True]
    assert len(first_bot.sent) + len(second_bot.sent) == 1

    async with session_scope(session_factory) as session:
        assert await SettingsRepository(session).get(DAILY_REPORT_SETTING) == "2026-10-14"


async def test_second_run_after_first_does_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bot = Bot()
    sender = DailyReportSender(bot, [ADMIN], SettingsRepository, _deps(session_factory), CFG)

    assert await sender.send_due(NOW) is True
    assert await sender.send_due(NOW) is False
    assert bot.sent == [ADMIN]


async def test_get_for_update_returns_stored_value(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        await SettingsRepository(session).set(DAILY_REPORT_SETTING, "2026-10-13")

    async with session_scope(session_factory) as session:
        repo = SettingsRepository(session)
        assert await repo.get_for_update(DAILY_REPORT_SETTING) == "2026-10-13"
        assert await repo.get_for_update("missing", "по умолчанию") == "по умолчанию"


async def test_locked_row_blocks_second_reader(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вторая транзакция ждёт коммита первой и видит уже обновлённое значение."""
    async with session_scope(session_factory) as session:
        await SettingsRepository(session).set(DAILY_REPORT_SETTING, "старое")

    released = asyncio.Event()

    async def holder() -> None:
        async with session_scope(session_factory) as session:
            repo = SettingsRepository(session)
            await repo.get_for_update(DAILY_REPORT_SETTING)
            await asyncio.sleep(0.05)
            await repo.set(DAILY_REPORT_SETTING, "новое")
            released.set()

    async def waiter() -> Any:
        await asyncio.sleep(0.01)
        async with session_scope(session_factory) as session:
            value = await SettingsRepository(session).get_for_update(DAILY_REPORT_SETTING)
        assert released.is_set(), "чтение не должно завершиться до коммита первой транзакции"
        return value

    _, seen = await asyncio.gather(holder(), waiter())
    assert seen == "новое"
