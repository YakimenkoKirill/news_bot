from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.config import DailyReportConfig, MetricsConfig
from app.reports import service
from app.reports.daily import (
    DailyContext,
    DailyReport,
    Errors,
    Funnel,
    Headline,
    Publications,
    Quality,
    QueueState,
    Spending,
)
from app.reports.metrics import Period
from app.reports.service import ReportDeps, ReportPeriod, build_report, periods
from tests.config_helpers import base_section

MSK = ZoneInfo("Europe/Moscow")
CFG = DailyReportConfig(**base_section("daily_report"))
METRICS = MetricsConfig(**base_section("metrics"))
BUDGET = Decimal("0.5")
LIMIT = 4096
CTX = DailyContext(tz=MSK, daily_budget=BUDGET, decay_per_h=0.02, top_posts=3, channel_id=-100)


def msk(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def test_today_runs_from_local_midnight_to_now() -> None:
    now = msk(2026, 10, 14, 15, 30)
    current, previous, title = periods(ReportPeriod.TODAY, now, MSK)
    assert current.start == msk(2026, 10, 14)
    assert current.end == now
    assert previous.start == msk(2026, 10, 13)
    assert previous.end == now - timedelta(days=1)
    assert title == "14 октября"


def test_today_just_after_midnight_is_short() -> None:
    now = msk(2026, 10, 14, 0, 5)
    current, _, _ = periods(ReportPeriod.TODAY, now, MSK)
    assert current.end - current.start == timedelta(minutes=5)


def test_today_uses_moscow_midnight_not_utc() -> None:
    now = datetime(2026, 10, 14, 0, 30, tzinfo=UTC)  # 03:30 по Москве
    current, _, title = periods(ReportPeriod.TODAY, now, MSK)
    assert current.start == msk(2026, 10, 14)
    assert title == "14 октября"


def test_late_evening_utc_is_next_moscow_day() -> None:
    now = datetime(2026, 10, 14, 22, 0, tzinfo=UTC)  # 01:00 15 октября по Москве
    _, _, title = periods(ReportPeriod.TODAY, now, MSK)
    assert title == "15 октября"


def test_yesterday_is_full_previous_day() -> None:
    now = msk(2026, 10, 14, 9, 0)
    current, previous, title = periods(ReportPeriod.YESTERDAY, now, MSK)
    assert (current.start, current.end) == (msk(2026, 10, 13), msk(2026, 10, 14))
    assert (previous.start, previous.end) == (msk(2026, 10, 12), msk(2026, 10, 13))
    assert title == "вчера, 13 октября"


def test_week_is_seven_full_days_without_today() -> None:
    now = msk(2026, 10, 14, 9, 0)
    current, previous, title = periods(ReportPeriod.WEEK, now, MSK)
    assert (current.start, current.end) == (msk(2026, 10, 7), msk(2026, 10, 14))
    assert current.end - current.start == timedelta(days=7)
    assert (previous.start, previous.end) == (msk(2026, 9, 30), msk(2026, 10, 7))
    assert title == "7 октября – 13 октября"


def test_week_across_month_boundary() -> None:
    now = msk(2026, 10, 5, 12, 0)
    current, _, title = periods(ReportPeriod.WEEK, now, MSK)
    assert current.start == msk(2026, 9, 28)
    assert title == "28 сентября – 4 октября"


@pytest.mark.parametrize("kind", list(ReportPeriod))
def test_previous_period_has_same_length(kind: ReportPeriod) -> None:
    current, previous, _ = periods(kind, msk(2026, 10, 14, 15, 0), MSK)
    assert previous.end - previous.start == current.end - current.start


@pytest.mark.parametrize("kind", [ReportPeriod.YESTERDAY, ReportPeriod.WEEK])
def test_full_periods_join_without_gap(kind: ReportPeriod) -> None:
    current, previous, _ = periods(kind, msk(2026, 10, 14, 15, 0), MSK)
    assert previous.end == current.start


def test_today_compares_with_same_hour_yesterday() -> None:
    """У «сегодня» предыдущий период — вчера до того же времени, а не целые сутки."""
    current, previous, _ = periods(ReportPeriod.TODAY, msk(2026, 10, 14, 15, 0), MSK)
    assert previous.end == msk(2026, 10, 13, 15, 0)
    assert previous.end < current.start


def _empty_report(period: Period) -> DailyReport:
    return DailyReport(
        period=period,
        publications=Publications(),
        sources=(),
        funnel=Funnel(),
        spending=Spending(),
        quality=Quality(),
        queue=QueueState(),
        errors=Errors(),
        previous=Headline(),
    )


class Recorder:
    """Подменяет collect_daily и format_daily, запоминая аргументы."""

    def __init__(self) -> None:
        self.collect_calls: list[tuple[Period, Period, bool]] = []
        self.format_calls: list[tuple[str, int]] = []

    async def collect_daily(
        self,
        session: Any,
        period: Period,
        previous: Period,
        ctx: DailyContext,
        *,
        paused: bool = False,
    ) -> DailyReport:
        self.collect_calls.append((period, previous, paused))
        return _empty_report(period)

    def format_daily(
        self,
        report: DailyReport,
        title: str,
        cfg: DailyReportConfig,
        text_limit: int,
        *,
        budget: Decimal,
        metrics: MetricsConfig,
    ) -> list[str]:
        self.format_calls.append((title, text_limit))
        return ["часть 1", "часть 2"]


def _deps(now: datetime, paused: bool = False) -> ReportDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[object]:
        yield object()

    async def is_paused(session: Any) -> bool:
        return paused

    return ReportDeps(
        session_scope=scope,
        context=lambda: CTX,
        cfg=CFG,
        metrics=METRICS,
        text_limit=LIMIT,
        budget=BUDGET,
        tz=MSK,
        clock=lambda: now,
        paused=is_paused,
    )


async def test_build_report_passes_periods_and_title(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    monkeypatch.setattr(service, "collect_daily", recorder.collect_daily)
    monkeypatch.setattr(service, "format_daily", recorder.format_daily)
    now = msk(2026, 10, 14, 15, 0)

    messages = await build_report(ReportPeriod.YESTERDAY, _deps(now))

    assert messages == ["часть 1", "часть 2"]
    [(period, previous, paused)] = recorder.collect_calls
    assert (period.start, period.end) == (msk(2026, 10, 13), msk(2026, 10, 14))
    assert previous.end == period.start
    assert paused is False
    assert recorder.format_calls == [("вчера, 13 октября", LIMIT)]


async def test_build_report_passes_paused_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    monkeypatch.setattr(service, "collect_daily", recorder.collect_daily)
    monkeypatch.setattr(service, "format_daily", recorder.format_daily)

    await build_report(ReportPeriod.TODAY, _deps(msk(2026, 10, 14, 9, 0), paused=True))
    assert recorder.collect_calls[0][2] is True


async def test_build_report_week_uses_same_code(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    monkeypatch.setattr(service, "collect_daily", recorder.collect_daily)
    monkeypatch.setattr(service, "format_daily", recorder.format_daily)

    await build_report(ReportPeriod.WEEK, _deps(msk(2026, 10, 14, 9, 0)))
    period, _, _ = recorder.collect_calls[0]
    assert period.end - period.start == timedelta(days=7)
    assert recorder.format_calls[0][0] == "7 октября – 13 октября"
