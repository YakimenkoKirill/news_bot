"""Сервис отчёта: выбор периода, подсчёт и текст — общий для /report и рассылки."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from app.config import DailyReportConfig, MetricsConfig
from app.reports.daily import DailyContext, collect_daily
from app.reports.daily_format import format_daily
from app.reports.metrics import Period

WEEK_DAYS = 7
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


class ReportPeriod(StrEnum):
    """Период отчёта."""

    TODAY = "today"
    YESTERDAY = "yesterday"
    WEEK = "week"


def _day_name(moment: datetime) -> str:
    return f"{moment.day} {MONTHS[moment.month - 1]}"


def _midnight(moment: datetime, tz: ZoneInfo) -> datetime:
    return datetime.combine(moment.astimezone(tz).date(), time.min, tzinfo=tz)


def periods(kind: ReportPeriod, now: datetime, tz: ZoneInfo) -> tuple[Period, Period, str]:
    """Период отчёта, предыдущий период той же длины и заголовок.

    Args:
        kind: Какой период нужен.
        now: Текущий момент.
        tz: Часовой пояс канала.

    Returns:
        Период, предыдущий период и заголовок для шапки отчёта.
    """
    today = _midnight(now, tz)
    if kind is ReportPeriod.TODAY:
        current = Period(start=today, end=now)
        previous = Period(start=today - timedelta(days=1), end=now - timedelta(days=1))
        return current, previous, _day_name(today)
    if kind is ReportPeriod.YESTERDAY:
        start = today - timedelta(days=1)
        current = Period(start=start, end=today)
        previous = Period(start=start - timedelta(days=1), end=start)
        return current, previous, f"вчера, {_day_name(start)}"
    start = today - timedelta(days=WEEK_DAYS)
    current = Period(start=start, end=today)
    previous = Period(start=start - timedelta(days=WEEK_DAYS), end=start)
    last = today - timedelta(days=1)
    return current, previous, f"{_day_name(start)} – {_day_name(last)}"


@dataclass(frozen=True)
class ReportDeps:
    """Зависимости отчёта.

    Attributes:
        session_scope: Открывает транзакцию БД (отчёт только читает).
        context: Собирает DailyContext (настройки подсчёта).
        cfg: Секция daily_report.
        metrics: Цели MVP для отметок в отчёте.
        text_limit: Лимит одного сообщения (publisher.text_limit).
        budget: Дневной бюджет на модель в долларах (ai.daily_budget).
        tz: Часовой пояс канала.
        clock: Текущее время.
        paused: Читает настройку паузы по открытой сессии.
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    context: Callable[[], DailyContext]
    cfg: DailyReportConfig
    metrics: MetricsConfig
    text_limit: int
    budget: Decimal
    tz: ZoneInfo
    clock: Callable[[], datetime]
    paused: Callable[[Any], Awaitable[bool]]


async def build_report(kind: ReportPeriod, deps: ReportDeps) -> list[str]:
    """Считает отчёт за период и собирает его текст.

    Args:
        kind: Период отчёта.
        deps: Зависимости.

    Returns:
        Сообщения отчёта, каждое в пределах лимита Telegram.
    """
    period, previous, title = periods(kind, deps.clock(), deps.tz)
    async with deps.session_scope() as session:
        paused = await deps.paused(session)
        report = await collect_daily(session, period, previous, deps.context(), paused=paused)
    return format_daily(
        report,
        title,
        deps.cfg,
        deps.text_limit,
        budget=deps.budget,
        metrics=deps.metrics,
    )
