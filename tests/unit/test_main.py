import asyncio
import logging
import os
import shutil
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from app import main as main_module
from app.config import CONFIGS_DIR, Settings, load_settings
from app.main import Application, Component

ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/news",
    "TELEGRAM_BOT_TOKEN": "token",
    "TELEGRAM_CHANNEL_ID": "-100",
    "TELEGRAM_ADMIN_IDS": "1",
    "TELETHON_API_ID": "1",
    "TELETHON_API_HASH": "hash",
    "DEEPSEEK_API_KEY": "key",
}


class FakeComponent:
    def __init__(
        self,
        name: str,
        calls: list[str],
        fail_start: bool = False,
        fail_stop: bool = False,
    ) -> None:
        self._name = name
        self.calls = calls
        self.fail_start = fail_start
        self.fail_stop = fail_stop

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        self.calls.append(f"start:{self._name}")
        if self.fail_start:
            raise RuntimeError(f"start {self._name}")

    async def stop(self) -> None:
        self.calls.append(f"stop:{self._name}")
        if self.fail_stop:
            raise RuntimeError(f"stop {self._name}")


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    root.handlers = handlers
    root.setLevel(level)
    structlog.reset_defaults()


def _components(calls: list[str], *names: str) -> list[Component]:
    return [FakeComponent(name, calls) for name in names]


async def test_start_in_order_stop_in_reverse() -> None:
    calls: list[str] = []
    app = Application(_components(calls, "a", "b", "c"))
    await app.start()
    await app.stop()
    assert calls == ["start:a", "start:b", "start:c", "stop:c", "stop:b", "stop:a"]


async def test_stop_twice_is_noop() -> None:
    calls: list[str] = []
    app = Application(_components(calls, "a"))
    await app.start()
    await app.stop()
    await app.stop()
    assert calls == ["start:a", "stop:a"]


async def test_start_failure_rolls_back_started() -> None:
    calls: list[str] = []
    components: list[Component] = [
        FakeComponent("a", calls),
        FakeComponent("b", calls),
        FakeComponent("c", calls, fail_start=True),
        FakeComponent("d", calls),
    ]
    app = Application(components)
    with pytest.raises(RuntimeError, match="start c"):
        await app.start()
    assert calls == ["start:a", "start:b", "start:c", "stop:b", "stop:a"]


async def test_stop_failure_does_not_block_others() -> None:
    calls: list[str] = []
    components: list[Component] = [
        FakeComponent("a", calls),
        FakeComponent("b", calls, fail_stop=True),
        FakeComponent("c", calls),
    ]
    app = Application(components)
    await app.start()
    await app.stop()
    assert calls[-3:] == ["stop:c", "stop:b", "stop:a"]


async def test_serve_stops_on_event() -> None:
    calls: list[str] = []
    app = Application(_components(calls, "a", "b"))
    stop_event = asyncio.Event()
    task = asyncio.create_task(app.serve(stop_event))
    await asyncio.sleep(0)
    assert calls == ["start:a", "start:b"]
    assert not task.done()
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    assert calls == ["start:a", "start:b", "stop:b", "stop:a"]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    configs = tmp_path / "configs"
    configs.mkdir()
    shutil.copy(CONFIGS_DIR / "base.yaml", configs / "base.yaml")
    return load_settings(configs, "dev")


async def test_run_stops_on_sigterm_and_repeated_signal(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    started = asyncio.Event()
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    class SlowStop(FakeComponent):
        async def start(self) -> None:
            await super().start()
            started.set()

        async def stop(self) -> None:
            await super().stop()
            stop_started.set()
            await release_stop.wait()

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "build_components", lambda: [SlowStop("slow", calls)])

    task = asyncio.create_task(main_module.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(stop_started.wait(), timeout=1)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.sleep(0)
    assert not task.done()
    release_stop.set()
    await asyncio.wait_for(task, timeout=1)

    out = capsys.readouterr().out
    assert '"app.started"' in out
    assert '"app.stopped"' in out
    assert calls == ["start:slow", "stop:slow"]
