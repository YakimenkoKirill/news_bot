import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from telethon.errors import FloodWaitError

from app.collectors.base import TemporaryCollectorError
from app.collectors.telegram import client as client_module
from app.collectors.telegram.client import (
    TelegramAuthRequired,
    build_client,
    connect_authorized,
    with_flood_wait,
)
from app.config import CONFIGS_DIR, Settings, load_settings
from app.logging import setup_logging
from tests.config_helpers import telegram_config

CONFIG = telegram_config(flood_wait_extra_s=1, flood_wait_max_retries=3, flood_wait_max_s=300)
API_HASH = "telethon-api-hash-secret"


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _flood(seconds: int) -> FloodWaitError:
    return FloodWaitError(request=None, capture=seconds)


class Flaky:
    """Вызов, который сначала бросает заданные ошибки, потом возвращает результат."""

    def __init__(self, errors: list[Exception], result: str = "ok") -> None:
        self.errors = list(errors)
        self.result = result
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


async def test_waits_on_flood_wait_then_succeeds() -> None:
    sleep = FakeSleep()
    call = Flaky([_flood(5), _flood(10)])
    assert await with_flood_wait(call, CONFIG, sleep=sleep) == "ok"
    assert call.calls == 3
    assert sleep.calls == [6, 11]


async def test_no_wait_without_flood() -> None:
    sleep = FakeSleep()
    assert await with_flood_wait(Flaky([]), CONFIG, sleep=sleep) == "ok"
    assert sleep.calls == []


async def test_retry_limit_exceeded() -> None:
    sleep = FakeSleep()
    call = Flaky([_flood(1)] * (CONFIG.flood_wait_max_retries + 1))
    with pytest.raises(TemporaryCollectorError, match="FloodWait"):
        await with_flood_wait(call, CONFIG, sleep=sleep)
    assert call.calls == CONFIG.flood_wait_max_retries + 1
    assert len(sleep.calls) == CONFIG.flood_wait_max_retries


async def test_too_long_wait_is_temporary_error_without_sleeping() -> None:
    sleep = FakeSleep()
    call = Flaky([_flood(int(CONFIG.flood_wait_max_s))])
    with pytest.raises(TemporaryCollectorError) as caught:
        await with_flood_wait(call, CONFIG, sleep=sleep)
    assert isinstance(caught.value.__cause__, FloodWaitError)
    assert sleep.calls == []
    assert call.calls == 1


async def test_wait_at_limit_is_allowed() -> None:
    sleep = FakeSleep()
    seconds = int(CONFIG.flood_wait_max_s - CONFIG.flood_wait_extra_s)
    assert await with_flood_wait(Flaky([_flood(seconds)]), CONFIG, sleep=sleep) == "ok"
    assert sleep.calls == [CONFIG.flood_wait_max_s]


async def test_other_errors_not_retried() -> None:
    sleep = FakeSleep()
    call = Flaky([ValueError("bad request")])
    with pytest.raises(ValueError, match="bad request"):
        await with_flood_wait(call, CONFIG, sleep=sleep)
    assert call.calls == 1


async def test_unauthorized_session_raises() -> None:
    client: Any = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=False)
    with pytest.raises(TelegramAuthRequired, match=r"telethon_login\.py"):
        await connect_authorized(client)
    client.disconnect.assert_awaited_once()


async def test_authorized_session_connects() -> None:
    client: Any = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    await connect_authorized(client)
    client.connect.assert_awaited_once()
    client.disconnect.assert_not_awaited()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    env = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
        "TELEGRAM_BOT_TOKEN": "bot-token",
        "TELEGRAM_CHANNEL_ID": "-100",
        "TELEGRAM_ADMIN_IDS": "1",
        "TELETHON_API_ID": "12345",
        "TELETHON_API_HASH": API_HASH,
        "TELETHON_SESSION_PATH": str(tmp_path / "data" / "telethon"),
        "DEEPSEEK_API_KEY": "key",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    configs = tmp_path / "configs"
    configs.mkdir()
    shutil.copy(CONFIGS_DIR / "base.yaml", configs / "base.yaml")
    return load_settings(configs, "dev")


def test_build_client_uses_session_and_hides_hash(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    created: dict[str, Any] = {}

    def fake_client(session: str, api_id: int, api_hash: str, **kwargs: Any) -> str:
        created.update(session=session, api_id=api_id, api_hash=api_hash, **kwargs)
        return "client"

    monkeypatch.setattr(client_module, "TelegramClient", fake_client)
    setup_logging("INFO", json_output=True)
    try:
        assert build_client(settings) == "client"
    finally:
        structlog.reset_defaults()
    assert created["session"] == str(settings.telethon_session_path)
    assert created["api_id"] == 12345
    assert created["api_hash"] == API_HASH
    assert created["flood_sleep_threshold"] == 0
    assert settings.telethon_session_path.parent.is_dir()
    out = capsys.readouterr().out
    assert "telegram.client_built" in out
    assert API_HASH not in out
    assert API_HASH not in repr(settings)
