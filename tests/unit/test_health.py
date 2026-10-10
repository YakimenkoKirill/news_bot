import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app import health
from app.config import CONFIGS_DIR, HealthConfig, Settings, load_settings
from app.health import HealthResult, check, evaluate_heartbeat, heartbeat_value
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
    async def read(engine: Any) -> Any:
        return heartbeat_value(NOW - timedelta(hours=1), NOW - timedelta(seconds=30))

    monkeypatch.setattr(health, "_read_heartbeat", read)
    assert await check(settings, now=NOW) == HealthResult(ok=True, reasons=[])


async def test_unreachable_db_without_traceback(settings: Settings) -> None:
    result = await check(settings, now=NOW)
    assert result.ok is False
    [reason] = result.reasons
    assert reason.startswith("БД недоступна")


async def test_db_timeout(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(engine: Any) -> Any:
        await asyncio.sleep(10)

    monkeypatch.setattr(health, "_read_heartbeat", slow)
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
