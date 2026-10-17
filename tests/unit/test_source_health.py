from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.collectors.base import TemporaryCollectorError
from app.collectors.health import SourceHealthTracker
from app.db.models import Source
from app.domain.alerts import AlertEvent, AlertKind
from app.domain.enums import SourceType
from tests.config_helpers import collectors_config

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CONFIG = collectors_config(max_consecutive_errors=3, stale_after_h=6)


class FakeRepo:
    def __init__(self) -> None:
        self.errors: dict[int, int] = {}
        self.seen: dict[int, datetime] = {}

    async def register_error(self, source_id: int) -> int:
        self.errors[source_id] = self.errors.get(source_id, 0) + 1
        return self.errors[source_id]

    async def reset_errors(self, source_id: int) -> bool:
        self.errors[source_id] = 0
        return True

    async def mark_item_seen(self, source_id: int, at: datetime) -> bool:
        self.seen[source_id] = at
        return True


class FakeSink:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    async def emit(self, event: AlertEvent) -> None:
        self.events.append(event)


@pytest.fixture
def repo() -> FakeRepo:
    return FakeRepo()


@pytest.fixture
def sink() -> FakeSink:
    return FakeSink()


@pytest.fixture
def tracker(repo: FakeRepo, sink: FakeSink) -> SourceHealthTracker:
    return SourceHealthTracker(repo, sink, CONFIG, clock=lambda: NOW)


def _source(
    source_id: int = 1,
    *,
    last_item_at: datetime | None = None,
    created_at: datetime = NOW - timedelta(days=30),
    enabled: bool = True,
) -> Source:
    return Source(
        id=source_id,
        type=SourceType.RSS,
        url=f"https://s{source_id}.example/rss",
        name=f"Источник {source_id}",
        interval_min=10,
        enabled=enabled,
        last_item_at=last_item_at,
        created_at=created_at,
    )


ERROR = TemporaryCollectorError("timeout")


async def test_alert_on_reaching_threshold(tracker: SourceHealthTracker, sink: FakeSink) -> None:
    source = _source()
    for _ in range(CONFIG.max_consecutive_errors - 1):
        await tracker.on_error(source, ERROR)
    assert sink.events == []
    assert await tracker.on_error(source, ERROR) == CONFIG.max_consecutive_errors
    [event] = sink.events
    assert event.kind is AlertKind.SOURCE_DOWN
    assert event.key == "source_down:1"
    assert "Источник 1" in event.message
    assert "timeout" in event.message
    assert event.created_at == NOW


async def test_alert_sent_once_per_series(tracker: SourceHealthTracker, sink: FakeSink) -> None:
    source = _source()
    for _ in range(CONFIG.max_consecutive_errors * 3):
        await tracker.on_error(source, ERROR)
    assert len(sink.events) == 1


async def test_success_resets_and_new_series_alerts_again(
    tracker: SourceHealthTracker, sink: FakeSink, repo: FakeRepo
) -> None:
    source = _source()
    for _ in range(CONFIG.max_consecutive_errors):
        await tracker.on_error(source, ERROR)
    await tracker.on_success(source, items_count=0, now=NOW)
    assert repo.errors[1] == 0
    assert 1 not in repo.seen
    for _ in range(CONFIG.max_consecutive_errors):
        await tracker.on_error(source, ERROR)
    assert [e.kind for e in sink.events] == [AlertKind.SOURCE_DOWN, AlertKind.SOURCE_DOWN]


async def test_success_with_items_marks_seen(tracker: SourceHealthTracker, repo: FakeRepo) -> None:
    await tracker.on_success(_source(), items_count=4, now=NOW)
    assert repo.seen[1] == NOW


async def test_errors_counted_per_source(tracker: SourceHealthTracker, sink: FakeSink) -> None:
    for _ in range(CONFIG.max_consecutive_errors - 1):
        await tracker.on_error(_source(1), ERROR)
        await tracker.on_error(_source(2), ERROR)
    assert sink.events == []
    await tracker.on_error(_source(2), ERROR)
    assert [e.key for e in sink.events] == ["source_down:2"]


async def test_stale_by_last_item(tracker: SourceHealthTracker, sink: FakeSink) -> None:
    stale = _source(1, last_item_at=NOW - timedelta(hours=7))
    fresh = _source(2, last_item_at=NOW - timedelta(hours=5))
    boundary = _source(3, last_item_at=NOW - timedelta(hours=6))
    disabled = _source(4, last_item_at=NOW - timedelta(days=3), enabled=False)
    alerted = await tracker.check_stale([stale, fresh, boundary, disabled], NOW)
    assert [s.id for s in alerted] == [1, 3]
    assert [e.key for e in sink.events] == ["source_stale:1", "source_stale:3"]
    assert sink.events[0].kind is AlertKind.SOURCE_STALE
    assert "7 ч" in sink.events[0].message


async def test_stale_alert_not_repeated_until_state_changes(
    tracker: SourceHealthTracker, sink: FakeSink
) -> None:
    source = _source(1, last_item_at=NOW - timedelta(hours=7))
    await tracker.check_stale([source], NOW)
    await tracker.check_stale([source], NOW + timedelta(hours=1))
    assert len(sink.events) == 1

    await tracker.on_success(source, items_count=2, now=NOW + timedelta(hours=1))
    source.last_item_at = NOW + timedelta(hours=1)
    await tracker.check_stale([source], NOW + timedelta(hours=2))
    assert len(sink.events) == 1
    await tracker.check_stale([source], NOW + timedelta(hours=8))
    assert len(sink.events) == 2


async def test_new_source_uses_created_at(tracker: SourceHealthTracker, sink: FakeSink) -> None:
    young = _source(1, created_at=NOW - timedelta(hours=5))
    old = _source(2, created_at=NOW - timedelta(hours=6, minutes=1))
    alerted = await tracker.check_stale([young, old], NOW)
    assert [s.id for s in alerted] == [2]


def test_alert_event_validation() -> None:
    with pytest.raises(ValidationError):
        AlertEvent(kind=AlertKind.AI_DOWN, key="k", message="m", created_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        AlertEvent.model_validate(
            {"kind": "unknown", "key": "k", "message": "m", "created_at": NOW}
        )
    assert [k.value for k in AlertKind] == [
        "source_down",
        "source_stale",
        "ai_down",
        "budget_exceeded",
        "publish_error",
        "job_failed",
        "ai_item_failed",
        "stats_unavailable",
    ]
