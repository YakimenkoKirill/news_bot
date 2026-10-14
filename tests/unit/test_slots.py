from datetime import UTC, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from app.config import PublisherConfig
from app.publisher.slots import in_quiet_hours, next_slot, queue_eta
from tests.config_helpers import base_section

MSK = "Europe/Moscow"
NIGHT = (time(23, 0), time(7, 0))
DAY = (time(13, 0), time(15, 0))
INTERVAL = 10


def msk(hour: int, minute: int = 0, day: int = 10) -> datetime:
    """Московское время 2026-10-<day> в UTC (Москва = UTC+3)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC) - timedelta(hours=3)


def test_no_history_no_quiet_hours() -> None:
    now = msk(12)
    assert next_slot(now, None, INTERVAL, None, MSK) == now


def test_interval_respected() -> None:
    now = msk(12)
    last = msk(11, 55)
    assert next_slot(now, last, INTERVAL, None, MSK) == msk(12, 5)
    assert next_slot(now, msk(11, 30), INTERVAL, None, MSK) == now


def test_quiet_hours_before_midnight() -> None:
    assert next_slot(msk(23, 30), None, INTERVAL, NIGHT, MSK) == msk(7, day=11)


def test_quiet_hours_after_midnight() -> None:
    assert next_slot(msk(3, 15), None, INTERVAL, NIGHT, MSK) == msk(7)


def test_interval_pushes_into_quiet_hours() -> None:
    last = msk(22, 55)
    assert next_slot(msk(22, 58), last, INTERVAL, NIGHT, MSK) == msk(7, day=11)


def test_boundaries() -> None:
    assert next_slot(msk(7), None, INTERVAL, NIGHT, MSK) == msk(7)
    assert next_slot(msk(23), None, INTERVAL, NIGHT, MSK) == msk(7, day=11)
    just_before = msk(22, 59)
    assert next_slot(just_before, None, INTERVAL, NIGHT, MSK) == just_before


def test_daytime_window_not_crossing_midnight() -> None:
    assert next_slot(msk(14), None, INTERVAL, DAY, MSK) == msk(15)
    assert next_slot(msk(16), None, INTERVAL, DAY, MSK) == msk(16)


def test_result_is_utc() -> None:
    result = next_slot(msk(3), None, INTERVAL, NIGHT, MSK)
    assert result.tzinfo is UTC
    assert result == datetime(2026, 10, 10, 4, 0, tzinfo=UTC)


def test_other_timezone() -> None:
    now = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)  # 15:00 в Токио (UTC+9)
    assert next_slot(now, None, INTERVAL, NIGHT, "Asia/Tokyo") == now
    tokyo_night = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)  # 00:00 в Токио
    assert next_slot(tokyo_night, None, INTERVAL, NIGHT, "Asia/Tokyo") == datetime(
        2026, 10, 10, 22, 0, tzinfo=UTC
    )
    assert next_slot(tokyo_night, None, INTERVAL, NIGHT, MSK) == tokyo_night


def test_equal_start_and_end_means_no_quiet_hours() -> None:
    assert not in_quiet_hours(time(3), (time(0), time(0)))


def test_config_quiet_hours_validation() -> None:
    base = base_section("publisher")
    assert PublisherConfig(**{**base, "quiet_hours": []}).quiet_window is None
    assert PublisherConfig(**{**base, "quiet_hours": ["23:00", "07:00"]}).quiet_window == NIGHT
    for bad in (["23:00"], ["25:00", "07:00"], ["23-00", "07:00"], ["1", "2", "3"]):
        with pytest.raises(ValidationError, match="quiet_hours"):
            PublisherConfig(**{**base, "quiet_hours": bad})


def test_config_score_decay() -> None:
    base = base_section("publisher")
    assert PublisherConfig(**base).score_decay_per_h == 0.02
    assert PublisherConfig(**{**base, "score_decay_per_h": 0}).score_decay_per_h == 0
    with pytest.raises(ValidationError, match="score_decay_per_h"):
        PublisherConfig(**{**base, "score_decay_per_h": -0.1})
    without = {key: value for key, value in base.items() if key != "score_decay_per_h"}
    with pytest.raises(ValidationError, match="score_decay_per_h"):
        PublisherConfig(**without)


def test_queue_eta_interval_without_history() -> None:
    now = msk(12)
    etas = queue_eta(now, None, 3, INTERVAL, None, MSK)
    assert etas == [now, now + timedelta(minutes=10), now + timedelta(minutes=20)]


def test_queue_eta_starts_after_last_publication() -> None:
    now = msk(12)
    etas = queue_eta(now, now - timedelta(minutes=4), 2, INTERVAL, NIGHT, MSK)
    assert etas == [msk(12, 6), msk(12, 16)]


def test_queue_eta_quiet_hours_after_midnight() -> None:
    etas = queue_eta(msk(22, 50), None, 3, INTERVAL, NIGHT, MSK)
    assert etas == [msk(22, 50), msk(7, 0, day=11), msk(7, 10, day=11)]


def test_queue_eta_first_post_waits_for_quiet_end() -> None:
    assert queue_eta(msk(2), None, 1, INTERVAL, NIGHT, MSK) == [msk(7)]


def test_queue_eta_zero_count() -> None:
    assert queue_eta(msk(12), None, 0, INTERVAL, NIGHT, MSK) == []
