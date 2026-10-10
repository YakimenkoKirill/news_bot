"""Клиент Telethon: подключение по готовой сессии и обработка FloodWait."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import structlog
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
)

from app.collectors.base import TemporaryCollectorError
from app.config import Settings, TelegramConfig

log = structlog.get_logger(__name__)

T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]

LOGIN_HINT = "python scripts/telethon_login.py"
NO_AUTO_FLOOD_SLEEP = 0


class TelegramAuthRequired(RuntimeError):
    """Сессия Telethon не авторизована: нужен разовый вход через скрипт."""


def build_client(settings: Settings) -> TelegramClient:
    """Создаёт клиент Telethon с файлом сессии из telethon_session_path.

    Автоматическое ожидание FloodWait внутри Telethon отключено: все ожидания
    идут через with_flood_wait с лимитами из конфигурации.
    """
    session_path = settings.telethon_session_path
    session_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("telegram.client_built", session=str(session_path), api_id=settings.telethon_api_id)
    return TelegramClient(
        str(session_path),
        settings.telethon_api_id,
        settings.telethon_api_hash.get_secret_value(),
        flood_sleep_threshold=NO_AUTO_FLOOD_SLEEP,
    )


async def connect_authorized(client: TelegramClient) -> None:
    """Подключает клиент по готовой сессии.

    Raises:
        TelegramAuthRequired: Сессия не авторизована (интерактивного входа в приложении нет).
    """
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise TelegramAuthRequired(
            f"Сессия Telethon не авторизована. Выполните разовый вход: {LOGIN_HINT}"
        )
    log.info("telegram.connected")


async def with_flood_wait(
    call_factory: Callable[[], Awaitable[T]],
    config: TelegramConfig,
    *,
    sleep: Sleep = asyncio.sleep,
) -> T:
    """Выполняет вызов Telegram, пережидая FloodWait.

    При FloodWaitError ждёт seconds + flood_wait_extra_s и повторяет вызов не больше
    flood_wait_max_retries раз. Если требуемое ожидание больше flood_wait_max_s или
    повторы исчерпаны, бросает TemporaryCollectorError.

    Args:
        call_factory: Создаёт новую корутину вызова для каждой попытки.
        config: Секция telegram.
        sleep: Функция ожидания (подменяется в тестах).

    Returns:
        Результат вызова.

    Raises:
        TemporaryCollectorError: Ожидание слишком долгое или повторы исчерпаны.
    """

    def wait_needed(error: BaseException) -> float:
        seconds = getattr(error, "seconds", 0)
        return float(seconds) + config.flood_wait_extra_s

    def retryable(error: BaseException) -> bool:
        return isinstance(error, FloodWaitError) and wait_needed(error) <= config.flood_wait_max_s

    def wait(state: RetryCallState) -> float:
        error = state.outcome.exception() if state.outcome else None
        delay = wait_needed(error) if error is not None else 0.0
        log.warning("telegram.flood_wait", attempt=state.attempt_number, delay_s=delay)
        return delay

    retrying: AsyncRetrying = AsyncRetrying(
        stop=stop_after_attempt(config.flood_wait_max_retries + 1),
        wait=wait,
        retry=retry_if_exception(retryable),
        sleep=sleep,
        reraise=True,
    )
    try:
        async for attempt in retrying:
            with attempt:
                return await call_factory()
    except FloodWaitError as exc:
        raise TemporaryCollectorError(
            f"FloodWait {exc.seconds} с: превышены лимиты ожидания или повторов"
        ) from exc
    raise AssertionError("AsyncRetrying завершился без результата")
