from datetime import UTC, date, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import MetricsConfig
from app.reports.metrics import MetricCheck, Period, RunReport, compare_with_targets, percentile
from tests.config_helpers import base_section

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")
CFG = MetricsConfig(**base_section("metrics"))


def _real_length(period: Period) -> timedelta:
    """Настоящая длительность периода: по стенным часам DST не виден."""
    return period.end.astimezone(UTC) - period.start.astimezone(UTC)


def test_percentile_single_value() -> None:
    assert percentile([7.5], 0.95) == 7.5


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([1.0, 2.0, 3.0, 4.0], 0.0, 1.0),
        ([1.0, 2.0, 3.0, 4.0], 1.0, 4.0),
        ([1.0, 2.0, 3.0, 4.0], 0.5, 2.5),
        ([10.0, 20.0], 0.25, 12.5),
    ],
)
def test_percentile_interpolates(values: list[float], q: float, expected: float) -> None:
    assert percentile(values, q) == pytest.approx(expected)


def test_percentile_ignores_input_order() -> None:
    assert percentile([5.0, 1.0, 3.0], 0.5) == percentile([1.0, 3.0, 5.0], 0.5)


def test_compare_without_data_gives_none() -> None:
    checks = compare_with_targets(RunReport(), CFG)
    assert [c.name for c in checks] == [
        "медиана задержки, мин",
        "доля отклонённых модератором",
        "доля дублей в канале",
    ]
    assert all(c.ok is None and c.value is None for c in checks)


def test_compare_within_targets() -> None:
    report = RunReport(
        delay_median_min=CFG.max_delay_min - 1,
        rejected_share=0.0,
        duplicate_share=0.0,
    )
    assert all(c.ok is True for c in compare_with_targets(report, CFG))


def test_compare_exact_target_is_ok() -> None:
    report = RunReport(delay_median_min=CFG.max_delay_min)
    assert compare_with_targets(report, CFG)[0] == MetricCheck(
        "медиана задержки, мин", CFG.max_delay_min, CFG.max_delay_min, True
    )


def test_compare_above_target_fails() -> None:
    report = RunReport(
        delay_median_min=CFG.max_delay_min + 1,
        rejected_share=CFG.max_rejected_share + 0.1,
        duplicate_share=CFG.max_duplicate_share + 0.1,
    )
    assert all(c.ok is False for c in compare_with_targets(report, CFG))


def test_period_day_is_local_midnight_to_midnight() -> None:
    period = Period.day(date(2026, 10, 14), MSK)
    assert period.start.isoformat() == "2026-10-14T00:00:00+03:00"
    assert period.end.isoformat() == "2026-10-15T00:00:00+03:00"
    assert _real_length(period) == timedelta(hours=24)


def test_period_day_matches_from_dates() -> None:
    day = date(2026, 10, 14)
    assert Period.day(day, MSK) == Period.from_dates(day, day, MSK)


def test_period_day_short_on_dst_start() -> None:
    period = Period.day(date(2026, 3, 29), BERLIN)  # перевод часов вперёд
    assert _real_length(period) == timedelta(hours=23)


def test_period_day_long_on_dst_end() -> None:
    period = Period.day(date(2026, 10, 25), BERLIN)  # перевод часов назад
    assert _real_length(period) == timedelta(hours=25)
