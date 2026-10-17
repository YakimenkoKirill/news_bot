from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from app.config import Settings, load_settings
from app.runtime_config import (
    OVERRIDES_SETTING,
    QUEUE_LIMIT_KEY,
    RUNTIME_KEYS,
    RuntimeConfig,
    key_names,
)
from tests.config_helpers import configs_copy

ADMIN = 111


@dataclass
class Db:
    settings: dict[str, Any] = field(default_factory=dict)
    reads: int = 0
    writes: list[Any] = field(default_factory=list)


class SettingsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, key: str, default: Any = None) -> Any:
        self.db.reads += 1
        return self.db.settings.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self.db.settings[key] = value
        self.db.writes.append(value)


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def tick(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    return load_settings(configs_copy(tmp_path, monkeypatch), "dev")


def _runtime(
    settings: Settings,
    db: Db | None = None,
    clock: Clock | None = None,
    changed: list[str] | None = None,
) -> tuple[RuntimeConfig, Db, Clock]:
    db = db or Db()
    clock = clock or Clock()

    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    async def on_change(key: str) -> None:
        if changed is not None:
            changed.append(key)

    runtime = RuntimeConfig(
        scope,
        SettingsRepo,
        settings,
        clock=clock,
        on_change=on_change if changed is not None else None,
    )
    return runtime, db, clock


async def test_snapshot_without_overrides_matches_yaml(settings: Settings) -> None:
    runtime, _, _ = _runtime(settings)
    snapshot = await runtime.snapshot()
    assert snapshot.channel == settings.channel
    assert snapshot.ai == settings.ai
    assert snapshot.publisher == settings.publisher


async def test_all_keys_listed() -> None:
    assert key_names() == [
        "channel.topic",
        "ai.min_score",
        "publisher.min_interval_min",
        "publisher.quiet_hours",
        "publisher.max_queued_per_source",
        "publisher.score_decay_per_h",
    ]
    assert len(RUNTIME_KEYS) == len(key_names())


@pytest.mark.parametrize(
    ("key", "raw", "section", "field_name", "expected"),
    [
        ("channel.topic", "спорт и финансы", "channel", "topic", "спорт и финансы"),
        ("ai.min_score", "0.8", "ai", "min_score", 0.8),
        ("ai.min_score", "0,8", "ai", "min_score", 0.8),
        ("publisher.min_interval_min", "15", "publisher", "min_interval_min", 15.0),
        (
            "publisher.quiet_hours",
            "23:00-07:00",
            "publisher",
            "quiet_hours",
            ["23:00", "07:00"],
        ),
        ("publisher.quiet_hours", "", "publisher", "quiet_hours", []),
        ("publisher.max_queued_per_source", "0", "publisher", "max_queued_per_source", 0),
        ("publisher.score_decay_per_h", "0", "publisher", "score_decay_per_h", 0.0),
    ],
)
async def test_set_applies_value(
    settings: Settings, key: str, raw: str, section: str, field_name: str, expected: Any
) -> None:
    runtime, db, _ = _runtime(settings)
    await runtime.set(key, raw, ADMIN)

    snapshot = await runtime.snapshot()
    assert getattr(getattr(snapshot, section), field_name) == expected
    assert db.settings[OVERRIDES_SETTING] == {key: expected}


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("channel.topic", "ок"),
        ("channel.topic", "т" * 201),
        ("ai.min_score", "1.5"),
        ("ai.min_score", "-0.1"),
        ("ai.min_score", "высокий"),
        ("publisher.min_interval_min", "0.5"),
        ("publisher.min_interval_min", "быстро"),
        ("publisher.quiet_hours", "23:00"),
        ("publisher.quiet_hours", "25:00-07:00"),
        ("publisher.quiet_hours", "23:00-07:00-08:00"),
        ("publisher.max_queued_per_source", "-1"),
        ("publisher.max_queued_per_source", "много"),
        ("publisher.score_decay_per_h", "-0.1"),
    ],
    ids=[
        "topic-short",
        "topic-long",
        "score-above-one",
        "score-negative",
        "score-text",
        "interval-too-small",
        "interval-text",
        "quiet-one-bound",
        "quiet-bad-time",
        "quiet-three-bounds",
        "limit-negative",
        "limit-text",
        "decay-negative",
    ],
)
async def test_bad_value_rejected_and_not_stored(settings: Settings, key: str, raw: str) -> None:
    runtime, db, _ = _runtime(settings)
    with pytest.raises(ValueError):
        await runtime.set(key, raw, ADMIN)
    assert db.settings == {}
    assert await runtime.snapshot() == runtime.base


async def test_unknown_key_rejected(settings: Settings) -> None:
    runtime, db, _ = _runtime(settings)
    with pytest.raises(ValueError, match="неизвестный параметр"):
        await runtime.set("publisher.caption_limit", "100", ADMIN)
    with pytest.raises(ValueError, match="неизвестный параметр"):
        await runtime.reset("publisher.caption_limit", ADMIN)
    assert db.settings == {}


async def test_snapshot_is_cached(settings: Settings) -> None:
    runtime, db, clock = _runtime(settings)
    await runtime.snapshot()
    reads = db.reads
    await runtime.snapshot()
    assert db.reads == reads, "в пределах cache_s читаем из кэша"

    clock.tick(settings.runtime.cache_s + 1)
    await runtime.snapshot()
    assert db.reads == reads + 1


async def test_set_resets_cache(settings: Settings) -> None:
    runtime, _, _ = _runtime(settings)
    await runtime.snapshot()
    await runtime.set("ai.min_score", "0.9", ADMIN)
    assert (await runtime.snapshot()).ai.min_score == 0.9


async def test_reset_returns_yaml_value(settings: Settings) -> None:
    runtime, db, _ = _runtime(settings)
    await runtime.set("ai.min_score", "0.9", ADMIN)
    await runtime.reset("ai.min_score", ADMIN)

    assert (await runtime.snapshot()).ai.min_score == settings.ai.min_score
    assert db.settings[OVERRIDES_SETTING] == {}


async def test_reset_of_untouched_key_is_noop(settings: Settings) -> None:
    runtime, db, _ = _runtime(settings)
    await runtime.reset("ai.min_score", ADMIN)
    assert db.writes == []
    assert (await runtime.snapshot()).ai.min_score == settings.ai.min_score


async def test_set_logs_old_and_new_value(settings: Settings) -> None:
    runtime, _, _ = _runtime(settings)
    with structlog.testing.capture_logs() as logs:
        await runtime.set("ai.min_score", "0.95", ADMIN)
    [entry] = [e for e in logs if e["event"] == "runtime.set"]
    assert entry["key"] == "ai.min_score"
    assert entry["old"] == repr(settings.ai.min_score)
    assert entry["new"] == repr(0.95)
    assert entry["admin_id"] == ADMIN


async def test_reset_logs_default(settings: Settings) -> None:
    runtime, _, _ = _runtime(settings)
    await runtime.set("ai.min_score", "0.95", ADMIN)
    with structlog.testing.capture_logs() as logs:
        await runtime.reset("ai.min_score", ADMIN)
    [entry] = [e for e in logs if e["event"] == "runtime.reset"]
    assert entry["old"] == repr(0.95)
    assert entry["new"] == repr(settings.ai.min_score)


async def test_broken_override_is_ignored(settings: Settings) -> None:
    """Значение правили руками в БД: параметр берётся из yaml, а не роняет задачу."""
    db = Db(settings={OVERRIDES_SETTING: {"ai.min_score": "не число"}})
    runtime, _, _ = _runtime(settings, db)

    with structlog.testing.capture_logs() as logs:
        snapshot = await runtime.snapshot()

    assert snapshot.ai.min_score == settings.ai.min_score
    assert [e for e in logs if e["event"] == "runtime.bad_override"]


async def test_unknown_override_is_ignored(settings: Settings) -> None:
    db = Db(settings={OVERRIDES_SETTING: {"publisher.caption_limit": 10}})
    runtime, _, _ = _runtime(settings, db)

    with structlog.testing.capture_logs() as logs:
        snapshot = await runtime.snapshot()

    assert snapshot.publisher == settings.publisher
    assert [e for e in logs if e["event"] == "runtime.unknown_override"]


async def test_broken_container_is_ignored(settings: Settings) -> None:
    db = Db(settings={OVERRIDES_SETTING: ["не словарь"]})
    runtime, _, _ = _runtime(settings, db)

    with structlog.testing.capture_logs() as logs:
        snapshot = await runtime.snapshot()

    assert snapshot.publisher == settings.publisher
    assert [e for e in logs if e["event"] == "runtime.bad_overrides"]


async def test_other_fields_of_section_kept(settings: Settings) -> None:
    runtime, _, _ = _runtime(settings)
    await runtime.set("publisher.min_interval_min", "20", ADMIN)
    publisher = (await runtime.snapshot()).publisher
    assert publisher.min_interval_min == 20
    assert publisher.caption_limit == settings.publisher.caption_limit
    assert publisher.footer_title == settings.publisher.footer_title


async def test_quiet_hours_window_recomputed(settings: Settings) -> None:
    """Тихие часы изменили — следующий слот считается по новым границам."""
    runtime, _, _ = _runtime(settings)
    await runtime.set("publisher.quiet_hours", "01:00-02:00", ADMIN)
    window = (await runtime.snapshot()).publisher.quiet_window
    assert window is not None
    assert (window[0].hour, window[1].hour) == (1, 2)

    await runtime.set("publisher.quiet_hours", "", ADMIN)
    assert (await runtime.snapshot()).publisher.quiet_window is None


async def test_queue_limit_change_notifies(settings: Settings) -> None:
    changed: list[str] = []
    runtime, _, _ = _runtime(settings, changed=changed)
    await runtime.set(QUEUE_LIMIT_KEY, "2", ADMIN)
    await runtime.set("ai.min_score", "0.7", ADMIN)
    await runtime.reset(QUEUE_LIMIT_KEY, ADMIN)
    assert changed == [QUEUE_LIMIT_KEY, "ai.min_score", QUEUE_LIMIT_KEY]


async def test_overrides_returns_stored_values(settings: Settings) -> None:
    runtime, _, _ = _runtime(settings)
    await runtime.set("ai.min_score", "0.75", ADMIN)
    assert await runtime.overrides() == {"ai.min_score": 0.75}
