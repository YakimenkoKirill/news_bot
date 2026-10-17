import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app import health
from app.config import CONFIGS_DIR, HealthConfig, Settings, load_settings
from app.health import (
    HealthResult,
    HealthState,
    check,
    evaluate_ai,
    evaluate_heartbeat,
    evaluate_state,
    evaluate_tg,
    heartbeat_value,
)
from tests.config_helpers import base_section

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CFG = HealthConfig(**base_section("health"))


def test_fresh_heartbeat_is_healthy() -> None:
    value = heartbeat_value(NOW - timedelta(hours=5), NOW - timedelta(seconds=CFG.max_age_s))
    assert evaluate_heartbeat(value, NOW, CFG) == []


def test_old_heartbeat() -> None:
    value = heartbeat_value(NOW - timedelta(hours=5), NOW - timedelta(seconds=CFG.max_age_s + 1))
    [reason] = evaluate_heartbeat(value, NOW, CFG)
    assert reason.startswith("heartbeat устарел: 181 с")


def test_no_heartbeat_within_startup_grace() -> None:
    value = heartbeat_value(NOW - timedelta(seconds=CFG.startup_grace_s), None)
    assert evaluate_heartbeat(value, NOW, CFG) == []


def test_no_heartbeat_after_startup_grace() -> None:
    value = heartbeat_value(NOW - timedelta(seconds=CFG.startup_grace_s + 1), None)
    [reason] = evaluate_heartbeat(value, NOW, CFG)
    assert "нет heartbeat" in reason


def test_old_heartbeat_but_fresh_restart_is_healthy() -> None:
    value = heartbeat_value(NOW - timedelta(seconds=10), NOW - timedelta(days=1))
    assert evaluate_heartbeat(value, NOW, CFG) == []


@pytest.mark.parametrize("value", [None, "2026-10-10T12:00:00", {"at": "мусор"}])
def test_missing_or_broken_value(value: Any) -> None:
    assert evaluate_heartbeat(value, NOW, CFG)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    env = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/db",
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHANNEL_ID": "-1",
        "TELEGRAM_ADMIN_IDS": "1",
        "TELETHON_API_ID": "1",
        "TELETHON_API_HASH": "h",
        "DEEPSEEK_API_KEY": "k",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    configs = tmp_path / "configs"
    configs.mkdir()
    shutil.copy(CONFIGS_DIR / "base.yaml", configs / "base.yaml")
    return load_settings(configs, "dev")


async def test_check_healthy(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def read(engine: Any, app_settings: Settings, now: Any) -> HealthState:
        return HealthState(
            heartbeat=heartbeat_value(NOW - timedelta(hours=1), NOW - timedelta(seconds=30))
        )

    monkeypatch.setattr(health, "_read", read)
    assert await check(settings, now=NOW) == HealthResult(ok=True, reasons=[])


async def test_unreachable_db_without_traceback(settings: Settings) -> None:
    result = await check(settings, now=NOW)
    assert result.ok is False
    [reason] = result.reasons
    assert reason.startswith("БД недоступна")


async def test_db_timeout(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(engine: Any, app_settings: Settings, now: Any) -> HealthState:
        await asyncio.sleep(10)
        return HealthState()

    monkeypatch.setattr(health, "_read", slow)
    fast = settings.model_copy(update={"health": CFG.model_copy(update={"db_timeout_s": 0.01})})
    result = await check(fast, now=NOW)
    assert result.reasons == ["БД не ответила за 0.01 с"]


@pytest.mark.parametrize(
    ("result", "code", "output"),
    [
        (HealthResult(ok=True), 0, "ok"),
        (
            HealthResult(ok=False, reasons=["БД недоступна: OSError"]),
            1,
            "unhealthy: БД недоступна: OSError",
        ),
    ],
)
def test_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: HealthResult,
    code: int,
    output: str,
) -> None:
    async def fake_check(settings: Any, **kwargs: Any) -> HealthResult:
        return result

    monkeypatch.setattr(health, "check", fake_check)
    monkeypatch.setattr(health, "get_settings", lambda: None)
    assert health.main() == code
    assert capsys.readouterr().out.strip() == output


def _state(**overrides: Any) -> HealthState:
    """Состояние с живым heartbeat и нужными переопределениями."""
    base: dict[str, Any] = {
        "heartbeat": heartbeat_value(NOW - timedelta(hours=5), NOW - timedelta(seconds=10)),
        "started_at": NOW - timedelta(hours=5),
        "has_tg_sources": True,
        "tg_alive_at": (NOW - timedelta(seconds=10)).isoformat(),
        "ai_ok_at": (NOW - timedelta(minutes=1)).isoformat(),
    }
    base.update(overrides)
    return HealthState(**base)


def test_fresh_tg_mark_is_healthy() -> None:
    state = _state()
    assert evaluate_tg(state.tg_alive_at, NOW, CFG, state) == []


def test_silent_telethon_is_a_problem() -> None:
    silence = CFG.tg_max_silence_s + 30
    state = _state(tg_alive_at=(NOW - timedelta(seconds=silence)).isoformat())
    [reason] = evaluate_tg(state.tg_alive_at, NOW, CFG, state)
    assert "Telethon не на связи" in reason
    assert f"молчит {int(silence)} с" in reason


def test_telethon_silence_at_limit_is_healthy() -> None:
    state = _state(tg_alive_at=(NOW - timedelta(seconds=CFG.tg_max_silence_s)).isoformat())
    assert evaluate_tg(state.tg_alive_at, NOW, CFG, state) == []


def test_telegram_not_checked_without_sources() -> None:
    """Нет включённых tg-источников — Telethon не нужен."""
    state = _state(has_tg_sources=False, tg_alive_at=None)
    assert evaluate_tg(state.tg_alive_at, NOW, CFG, state) == []


def test_missing_tg_mark_within_grace() -> None:
    state = _state(tg_alive_at=None, started_at=NOW - timedelta(seconds=CFG.startup_grace_s - 1))
    assert evaluate_tg(state.tg_alive_at, NOW, CFG, state) == []


def test_missing_tg_mark_after_grace() -> None:
    state = _state(tg_alive_at=None)
    [reason] = evaluate_tg(state.tg_alive_at, NOW, CFG, state)
    assert "отметки нет" in reason


def test_broken_tg_mark_is_a_problem() -> None:
    state = _state(tg_alive_at="вчера")
    assert evaluate_tg(state.tg_alive_at, NOW, CFG, state) != []


def test_fresh_ai_mark_is_healthy() -> None:
    state = _state(ai_backlog=True)
    assert evaluate_ai(state.ai_ok_at, NOW, CFG, state) == []


def test_stalled_model_with_backlog_is_a_problem() -> None:
    state = _state(
        ai_backlog=True,
        ai_ok_at=(NOW - timedelta(minutes=CFG.ai_backlog_min + 5)).isoformat(),
    )
    [reason] = evaluate_ai(state.ai_ok_at, NOW, CFG, state)
    assert "модель не обрабатывает очередь" in reason
    assert "последний ответ 35 мин назад" in reason


def test_model_not_checked_without_backlog() -> None:
    """Очередь normalized пуста — модели нечего обрабатывать."""
    state = _state(ai_backlog=False, ai_ok_at=(NOW - timedelta(days=3)).isoformat())
    assert evaluate_ai(state.ai_ok_at, NOW, CFG, state) == []


def test_pause_is_not_a_problem() -> None:
    state = _state(ai_backlog=True, paused=True, ai_ok_at=None)
    assert evaluate_ai(state.ai_ok_at, NOW, CFG, state) == []


def test_exhausted_budget_is_not_a_problem() -> None:
    state = _state(ai_backlog=True, budget_exhausted=True, ai_ok_at=None)
    assert evaluate_ai(state.ai_ok_at, NOW, CFG, state) == []


def test_missing_ai_mark_within_grace() -> None:
    state = _state(
        ai_backlog=True,
        ai_ok_at=None,
        started_at=NOW - timedelta(seconds=CFG.startup_grace_s - 1),
    )
    assert evaluate_ai(state.ai_ok_at, NOW, CFG, state) == []


def test_missing_ai_mark_after_grace() -> None:
    state = _state(ai_backlog=True, ai_ok_at=None)
    [reason] = evaluate_ai(state.ai_ok_at, NOW, CFG, state)
    assert "ответов нет" in reason


def test_evaluate_state_groups_reasons() -> None:
    state = _state(
        tg_alive_at=(NOW - timedelta(hours=2)).isoformat(),
        ai_backlog=True,
        ai_ok_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    problems = evaluate_state(state, NOW, CFG)
    assert set(problems) == {"heartbeat", "telegram", "ai"}
    assert problems["heartbeat"] == []
    assert problems["telegram"] and problems["ai"]


def test_healthy_state_has_no_reasons() -> None:
    assert all(not reasons for reasons in evaluate_state(_state(), NOW, CFG).values())
