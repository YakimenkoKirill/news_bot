"""Точка входа: жизненный цикл компонентов и остановка по сигналам."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Sequence
from typing import Protocol

import structlog

from app.config import get_settings
from app.logging import setup_logging

log = structlog.get_logger(__name__)

STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class Component(Protocol):
    """Компонент приложения с асинхронным запуском и остановкой."""

    @property
    def name(self) -> str:
        """Имя компонента для логов."""
        raise NotImplementedError

    async def start(self) -> None:
        """Запускает компонент."""

    async def stop(self) -> None:
        """Останавливает компонент."""


class Application:
    """Запускает компоненты по порядку и останавливает в обратном порядке."""

    def __init__(self, components: Sequence[Component]) -> None:
        self._components = list(components)
        self._started: list[Component] = []

    @property
    def components(self) -> list[Component]:
        """Компоненты в порядке запуска."""
        return list(self._components)

    async def start(self) -> None:
        """Запускает все компоненты.

        Если start() компонента упал, уже запущенные компоненты останавливаются,
        а исключение пробрасывается дальше.
        """
        for component in self._components:
            try:
                await component.start()
            except Exception:
                log.exception("component.start_failed", component=component.name)
                await self.stop()
                raise
            self._started.append(component)
            log.info("component.started", component=component.name)

    async def stop(self) -> None:
        """Останавливает запущенные компоненты в обратном порядке.

        Ошибка в stop() одного компонента логируется и не мешает остановить остальные.
        """
        while self._started:
            component = self._started.pop()
            try:
                await component.stop()
            except Exception:
                log.exception("component.stop_failed", component=component.name)
            else:
                log.info("component.stopped", component=component.name)

    async def serve(self, stop_event: asyncio.Event) -> None:
        """Запускает компоненты, ждёт stop_event и останавливает их.

        Args:
            stop_event: Событие, по которому начинается остановка.
        """
        await self.start()
        log.info("app.started", components=[c.name for c in self._components])
        try:
            await stop_event.wait()
        finally:
            await self.stop()
            log.info("app.stopped")


def build_components() -> list[Component]:
    """Возвращает компоненты приложения в порядке запуска."""
    return []


async def run() -> None:
    """Загружает настройки, настраивает логи и работает до SIGINT/SIGTERM.

    Повторный сигнал во время остановки только повторно выставляет событие
    и не прерывает остановку.
    """
    settings = get_settings()
    setup_logging(settings.log_level, json_output=True)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in STOP_SIGNALS:
        loop.add_signal_handler(sig, stop_event.set)
    try:
        await Application(build_components()).serve(stop_event)
    finally:
        for sig in STOP_SIGNALS:
            loop.remove_signal_handler(sig)


def main() -> None:
    """Запускает приложение в новом цикле событий."""
    asyncio.run(run())
