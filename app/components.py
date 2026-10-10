"""Компоненты приложения с запуском и остановкой (протокол Component из app.main)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.scheduler import JobRunner

log = structlog.get_logger(__name__)


class DatabaseComponent:
    """Движок БД: проверка соединения при старте, освобождение пула при остановке."""

    name = "database"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def start(self) -> None:
        """Проверяет, что БД отвечает."""
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def stop(self) -> None:
        """Закрывает соединения пула."""
        await self._engine.dispose()


class ClosingComponent:
    """Ресурс без запуска, который нужно закрыть при остановке (например, клиент API)."""

    def __init__(self, name: str, close: Callable[[], Awaitable[Any]]) -> None:
        self.name = name
        self._close = close

    async def start(self) -> None:
        """Ничего не делает: ресурс готов после создания."""

    async def stop(self) -> None:
        """Закрывает ресурс."""
        await self._close()


class BotPollingComponent:
    """Бот управления: регистрация команд и long polling в фоновой задаче."""

    name = "bot"

    def __init__(
        self,
        bot: Any,
        dispatcher: Any,
        on_start: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> None:
        self._bot = bot
        self._dispatcher = dispatcher
        self._on_start = on_start
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Регистрирует команды и запускает polling (без собственных обработчиков сигналов)."""
        if self._on_start is not None:
            await self._on_start(self._bot)
        self._task = asyncio.create_task(
            self._dispatcher.start_polling(self._bot, handle_signals=False, close_bot_session=False)
        )

    async def stop(self) -> None:
        """Останавливает polling и закрывает сессию бота."""
        try:
            if self._task is not None and not self._task.done():
                await self._dispatcher.stop_polling()
                await self._task
        finally:
            self._task = None
            await self._bot.session.close()


class SchedulerComponent:
    """Планировщик: при остановке не принимает новых запусков и ждёт текущие задачи."""

    name = "scheduler"

    def __init__(self, scheduler: Any, runner: JobRunner) -> None:
        self._scheduler = scheduler
        self._runner = runner

    async def start(self) -> None:
        """Запускает планировщик в текущем цикле событий."""
        self._scheduler.start()

    async def stop(self) -> None:
        """Останавливает планировщик и дожидается выполняющихся задач."""
        self._scheduler.shutdown(wait=False)
        await self._runner.wait_idle()
