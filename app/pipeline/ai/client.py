"""Клиент DeepSeek (OpenAI-совместимый API) с ограничением параллельности и повторами."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import openai
import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import AiConfig, Settings

log = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Message = dict[str, str]

RETRYABLE_ERRORS = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
)
JSON_RESPONSE_FORMAT = {"type": "json_object"}


class AiError(Exception):
    """Базовая ошибка обращения к модели."""


class AiFatalError(AiError):
    """Ошибка, которую повтор не исправит: ключ, запрос, модель, баланс."""


class AiUnavailableError(AiError):
    """API недоступен: повторы исчерпаны."""


class AiResponseError(AiError):
    """Ответ модели пустой или некорректный."""


@dataclass(frozen=True)
class AiRawResponse:
    """Текст ответа модели и расход токенов."""

    content: str
    prompt_tokens: int
    completion_tokens: int


class DeepSeekClient:
    """Обёртка над openai.AsyncOpenAI для DeepSeek.

    Не больше max_concurrency запросов одновременно; повторы с экспоненциальной
    задержкой на лимитах, сетевых ошибках, таймаутах и 5xx. Встроенные повторы
    клиента openai отключены.
    """

    def __init__(
        self,
        settings: Settings,
        cfg: AiConfig,
        openai_client: Any | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._cfg = cfg
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(cfg.max_concurrency)
        self._client: Any = openai_client or openai.AsyncOpenAI(
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=settings.deepseek_base_url,
            timeout=cfg.timeout_s,
            max_retries=0,
        )

    async def chat_json(self, messages: Sequence[Message]) -> AiRawResponse:
        """Отправляет диалог и просит ответ в формате JSON-объекта.

        Args:
            messages: Сообщения в формате OpenAI (role, content).

        Returns:
            Текст ответа и число токенов запроса и ответа.

        Raises:
            AiFatalError: Ключ неверен, запрос отклонён (400, 401, 403, 404 и т. п.).
            AiUnavailableError: Повторы на лимитах, сетевых ошибках и 5xx исчерпаны.
            AiResponseError: Ответ пустой.
        """
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._cfg.max_retries + 1),
            wait=self._wait,
            retry=retry_if_exception_type(RETRYABLE_ERRORS),
            sleep=self._sleep,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    response = await self._request(messages)
        except RetryError as exc:
            last = exc.last_attempt.exception()
            raise AiUnavailableError(f"API модели недоступен: {last!r}") from last
        except openai.APIStatusError as exc:
            raise AiFatalError(
                f"запрос к модели отклонён: HTTP {exc.status_code}: {exc.message}"
            ) from exc
        return self._parse(response)

    async def _request(self, messages: Sequence[Message]) -> Any:
        async with self._semaphore:
            return await self._client.chat.completions.create(
                model=self._cfg.model,
                messages=list(messages),
                temperature=self._cfg.temperature,
                response_format=JSON_RESPONSE_FORMAT,
                timeout=self._cfg.timeout_s,
            )

    def _parse(self, response: Any) -> AiRawResponse:
        choices = getattr(response, "choices", None) or []
        content = choices[0].message.content if choices else None
        if not content or not str(content).strip():
            raise AiResponseError("модель вернула пустой ответ")
        usage = getattr(response, "usage", None)
        if usage is None:
            log.warning("ai.usage_missing", model=self._cfg.model)
            return AiRawResponse(content=str(content), prompt_tokens=0, completion_tokens=0)
        return AiRawResponse(
            content=str(content),
            prompt_tokens=int(usage.prompt_tokens or 0),
            completion_tokens=int(usage.completion_tokens or 0),
        )

    def _wait(self, state: RetryCallState) -> float:
        delay = float(wait_exponential()(state))
        error = state.outcome.exception() if state.outcome else None
        log.warning("ai.retry", attempt=state.attempt_number, delay_s=delay, error=repr(error))
        return delay
