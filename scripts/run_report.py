"""Отчёт по метрикам тестового прогона и сравнение с целями MVP.

Запуск (даты включительно, в часовом поясе app.timezone):

    .venv/bin/python scripts/run_report.py --from 2026-10-10 --to 2026-10-14
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import date
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.base import create_engine, create_session_factory
from app.reports.metrics import (
    DUPLICATE_PREFIX,
    NO_DATA,
    P95,
    REVIEWED_STATUSES,
    MetricCheck,
    Period,
    RunReport,
    collect_report,
    compare_with_targets,
    percentile,
)

__all__ = [
    "DUPLICATE_PREFIX",
    "NO_DATA",
    "P95",
    "REVIEWED_STATUSES",
    "MetricCheck",
    "Period",
    "RunReport",
    "collect_report",
    "compare_with_targets",
    "format_report",
    "main",
    "percentile",
]


def _fmt(value: float | None) -> str:
    return NO_DATA if value is None else f"{value:.2f}"


def _minutes(value: float | None) -> str:
    return NO_DATA if value is None else f"{value:.2f} мин"


def format_report(report: RunReport, checks: Sequence[MetricCheck]) -> str:
    """Текст отчёта: метрики, расход по дням, ошибки источников и таблица целей."""
    duplicates = ", ".join(f"{k}: {v}" for k, v in sorted(report.duplicates_by_level.items()))
    lines = [
        f"Публикаций: {report.publications}",
        f"Задержка: медиана {_minutes(report.delay_median_min)}, "
        f"p95 {_minutes(report.delay_p95_min)}",
        f"Доля отклонённых модератором: {_fmt(report.rejected_share)}",
        f"Пойманные дубли: {duplicates or NO_DATA}",
        "Расход DeepSeek по дням:",
    ]
    if report.cost_by_day:
        days = sorted(report.cost_by_day.items())
        lines.extend(f"  {day.isoformat()}: {cost} $" for day, cost in days)
    else:
        lines.append(f"  {NO_DATA}")
    lines.append("Ошибки источников:")
    if report.source_errors:
        lines.extend(f"  {name}: {errors}" for name, errors in report.source_errors.items())
    else:
        lines.append("  нет")
    lines.append("")
    lines.append("метрика | значение | цель | итог")
    for item in checks:
        verdict = NO_DATA if item.ok is None else ("ok" if item.ok else "fail")
        lines.append(f"{item.name} | {_fmt(item.value)} | ≤ {item.target:g} | {verdict}")
    return "\n".join(lines)


async def _run(first: date, last: date) -> str:
    settings = get_settings()
    tz = ZoneInfo(settings.app.timezone)
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            report = await collect_report(session, Period.from_dates(first, last, tz), tz)
    finally:
        await engine.dispose()
    return format_report(report, compare_with_targets(report, settings.metrics))


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа скрипта."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="last", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    if args.last < args.first:
        print("Ошибка: --to раньше --from", file=sys.stderr)
        return 1
    print(asyncio.run(_run(args.first, args.last)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
