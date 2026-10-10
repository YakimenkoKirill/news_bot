"""HTTP-клиент сборщиков: таймауты, лимит запросов на домен и повторы."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from urllib.parse import urlsplit

import aiohttp
import structlog
from multidict import CIMultiDict, CIMultiDictProxy
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.collectors.base import PermanentCollectorError, TemporaryCollectorError
from app.config import CollectorsConfig

log = structlog.get_logger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]


@dataclass(frozen=True)
class HttpResponse:
    """Ответ сервера после всех редиректов; заголовки без учёта регистра."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


class _RetryableStatus(Exception):
    """Ответ 5xx или 429, после которого запрос стоит повторить."""

    def __init__(self, status: int, url: str, retry_after: float | None = None) -> None:
        super().__init__(f"HTTP {status} для {url}")
        self.status = status
        self.retry_after = retry_after


RETRYABLE_ERRORS = (_RetryableStatus, asyncio.TimeoutError, aiohttp.ClientConnectionError)


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Разбирает заголовок Retry-After: число секунд или HTTP-дата.

    Args:
        value: Значение заголовка.
        now: Текущий момент (timezone-aware) для вычисления задержки до даты.

    Returns:
        Задержка в секундах (не меньше 0) или None, если заголовок пуст или некорректен.
    """
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        log.warning("http.bad_retry_after", value=text)
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - now).total_seconds())


class _DomainRateLimiter:
    """Разносит запросы к одному домену не чаще rps в секунду."""

    def __init__(self, rps: float, clock: Clock, sleep: Sleep) -> None:
        self._interval = 1 / rps
        self._clock = clock
        self._sleep = sleep
        self._next_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = self._clock()
            next_at = self._next_at.get(host, now)
            if next_at > now:
                await self._sleep(next_at - now)
            self._next_at[host] = max(now, next_at) + self._interval


class HttpClient:
    """Общий HTTP-клиент процесса поверх одной aiohttp.ClientSession.

    Повторяет запрос с экспоненциальной задержкой на 5xx, таймаутах и ошибках
    соединения; на 429 ждёт Retry-After. Прочие 4xx — PermanentCollectorError,
    исчерпание повторов — TemporaryCollectorError.
    """

    name = "http_client"

    def __init__(
        self,
        config: CollectorsConfig,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        now: Now | None = None,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._limiter = _DomainRateLimiter(config.per_domain_rps, clock, sleep)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Открывает сессию; повторный вызов ничего не делает."""
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self._config.http_timeout_s)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        """Закрывает сессию."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def stop(self) -> None:
        """Останавливает клиент (протокол Component)."""
        await self.close()

    async def get(self, url: str, headers: Mapping[str, str] | None = None) -> HttpResponse:
        """Выполняет GET с лимитом на домен и повторами.

        Args:
            url: Адрес запроса.
            headers: Дополнительные заголовки (например, If-None-Match).

        Returns:
            Ответ 2xx или 3xx (в том числе 304) после редиректов.

        Raises:
            PermanentCollectorError: Ответ 4xx (кроме 429) или некорректный адрес.
            TemporaryCollectorError: Повторы исчерпаны или прочая сетевая ошибка.
        """
        if self._session is None:
            raise RuntimeError("HttpClient не запущен: вызовите start()")
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._config.max_retries + 1),
            wait=self._wait,
            retry=retry_if_exception_type(RETRYABLE_ERRORS),
            sleep=self._sleep,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self._get_once(self._session, url, headers)
        except RetryError as exc:
            last = exc.last_attempt.exception()
            raise TemporaryCollectorError(f"повторы исчерпаны для {url}: {last}") from last
        except aiohttp.InvalidURL as exc:
            raise PermanentCollectorError(f"некорректный адрес: {url}") from exc
        except aiohttp.ClientError as exc:
            raise TemporaryCollectorError(f"ошибка запроса {url}: {exc}") from exc
        raise AssertionError("AsyncRetrying завершился без результата")

    async def _get_once(
        self,
        session: aiohttp.ClientSession,
        url: str,
        headers: Mapping[str, str] | None,
    ) -> HttpResponse:
        await self._limiter.wait(urlsplit(url).netloc.lower())
        request_headers = {"User-Agent": self._config.user_agent, **(headers or {})}
        async with session.get(url, headers=request_headers) as resp:
            status = resp.status
            if status == HTTPStatus.TOO_MANY_REQUESTS:
                retry_after = parse_retry_after(resp.headers.get("Retry-After"), self._now())
                raise _RetryableStatus(status, url, retry_after)
            if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                raise _RetryableStatus(status, url)
            if status >= HTTPStatus.BAD_REQUEST:
                raise PermanentCollectorError(f"HTTP {status} для {url}")
            body = await resp.read()
            return HttpResponse(
                status=status,
                headers=CIMultiDictProxy(CIMultiDict(resp.headers)),
                body=body,
                url=str(resp.url),
            )

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _RetryableStatus) and exc.retry_after is not None:
            delay = exc.retry_after
        else:
            delay = wait_exponential()(state)
        log.warning(
            "http.retry",
            attempt=state.attempt_number,
            delay_s=delay,
            error=repr(exc),
        )
        return delay
