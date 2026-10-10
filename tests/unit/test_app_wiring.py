import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app import main as main_module
from app.collectors.telegram.client import TelegramAuthRequired
from app.components import (
    BotPollingComponent,
    ClosingComponent,
    DatabaseComponent,
    SchedulerComponent,
)
from app.config import CONFIGS_DIR, Settings, load_settings
from app.domain.alerts import AlertEvent
from app.main import Application, Component, build_components
from app.scheduler import JobRunner

EXPECTED_ORDER = [
    "database",
    "start_marker",
    "http_client",
    "ai_client",
    "bot",
    "telegram_collector",
    "scheduler",
]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    env = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:1/db",
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "TELEGRAM_CHANNEL_ID": "-100",
        "TELEGRAM_ADMIN_IDS": "1",
        "TELETHON_API_ID": "1",
        "TELETHON_API_HASH": "hash",
        "TELETHON_SESSION_PATH": str(tmp_path / "data" / "telethon"),
        "DEEPSEEK_API_KEY": "key",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    configs = tmp_path / "configs"
    configs.mkdir()
    shutil.copy(CONFIGS_DIR / "base.yaml", configs / "base.yaml")
    return load_settings(configs, "dev")


async def test_build_components_order_and_wiring(settings: Settings) -> None:
    components = build_components(settings)
    try:
        assert [c.name for c in components] == EXPECTED_ORDER
        assert isinstance(components[0], DatabaseComponent)
        assert isinstance(components[3], ClosingComponent)
        assert isinstance(components[4], BotPollingComponent)
        assert isinstance(components[6], SchedulerComponent)
    finally:
        await components[5].stop()
        await components[4].stop()
        await components[3].stop()
        await components[0].stop()


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


class FakeComponent:
    def __init__(self, name: str, rec: Recorder, error: Exception | None = None) -> None:
        self.name = name
        self.rec = rec
        self.error = error

    async def start(self) -> None:
        self.rec.calls.append(f"start:{self.name}")
        if self.error:
            raise self.error

    async def stop(self) -> None:
        self.rec.calls.append(f"stop:{self.name}")


async def test_start_and_stop_order() -> None:
    rec = Recorder()
    components: list[Component] = [FakeComponent(n, rec) for n in EXPECTED_ORDER]
    app = Application(components)
    await app.start()
    await app.stop()
    assert rec.calls == [f"start:{n}" for n in EXPECTED_ORDER] + [
        f"stop:{n}" for n in reversed(EXPECTED_ORDER)
    ]


async def test_telethon_auth_error_rolls_back() -> None:
    rec = Recorder()
    error = TelegramAuthRequired("Сессия Telethon не авторизована")
    components: list[Component] = [
        FakeComponent(n, rec, error if n == "telegram_collector" else None) for n in EXPECTED_ORDER
    ]
    with pytest.raises(TelegramAuthRequired):
        await Application(components).start()
    assert rec.calls == [
        "start:database",
        "start:start_marker",
        "start:http_client",
        "start:ai_client",
        "start:bot",
        "start:telegram_collector",
        "stop:bot",
        "stop:ai_client",
        "stop:http_client",
        "stop:start_marker",
        "stop:database",
    ]


def test_main_prints_clear_message_on_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_run() -> None:
        raise TelegramAuthRequired("выполните python scripts/telethon_login.py")

    monkeypatch.setattr(main_module, "run", failing_run)
    with pytest.raises(SystemExit) as caught:
        main_module.main()
    assert "telethon_login.py" in str(caught.value.code)


class FakeEngine:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.disposed = False

    def connect(self) -> Any:
        engine = self

        class Conn:
            async def __aenter__(self) -> "Conn":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def execute(self, statement: Any) -> None:
                engine.executed.append(str(statement))

        return Conn()

    async def dispose(self) -> None:
        self.disposed = True


async def test_database_component() -> None:
    engine = FakeEngine()
    component = DatabaseComponent(engine)  # type: ignore[arg-type]
    await component.start()
    await component.stop()
    assert engine.executed == ["SELECT 1"]
    assert engine.disposed


class FakeDispatcher:
    def __init__(self) -> None:
        self.stopped = asyncio.Event()
        self.kwargs: dict[str, Any] = {}

    async def start_polling(self, bot: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        await self.stopped.wait()

    async def stop_polling(self) -> None:
        self.stopped.set()


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_bot_polling_component() -> None:
    bot = type("Bot", (), {"session": FakeSession()})()
    dispatcher = FakeDispatcher()
    registered: list[Any] = []

    async def on_start(b: Any) -> None:
        registered.append(b)

    component = BotPollingComponent(bot, dispatcher, on_start)
    await component.start()
    await asyncio.sleep(0)
    assert registered == [bot]
    assert dispatcher.kwargs == {"handle_signals": False, "close_bot_session": False}
    await asyncio.wait_for(component.stop(), timeout=1)
    assert dispatcher.stopped.is_set()
    assert bot.session.closed


class Sink:
    async def emit(self, event: AlertEvent) -> None:
        return None


async def test_scheduler_component_waits_for_running_jobs() -> None:
    class FakeScheduler:
        def __init__(self) -> None:
            self.started = False
            self.shutdown_calls: list[bool] = []

        def start(self) -> None:
            self.started = True

        def shutdown(self, wait: bool = True) -> None:
            self.shutdown_calls.append(wait)

    runner = JobRunner(Sink(), 3, lambda: datetime(2026, 10, 10, tzinfo=UTC))
    release = asyncio.Event()
    finished: list[str] = []

    async def long_job(now: datetime) -> None:
        await release.wait()
        finished.append("job")

    scheduler = FakeScheduler()
    component = SchedulerComponent(scheduler, runner)
    await component.start()
    assert scheduler.started
    job = asyncio.create_task(runner.run("long", long_job))
    await asyncio.sleep(0)
    stopping = asyncio.create_task(component.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await asyncio.wait_for(stopping, timeout=1)
    await job
    assert finished == ["job"]
    assert scheduler.shutdown_calls == [False]


async def test_closing_component() -> None:
    closed: list[str] = []

    async def close() -> None:
        closed.append("x")

    component = ClosingComponent("ai_client", close)
    await component.start()
    await component.stop()
    assert closed == ["x"]
