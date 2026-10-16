import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
import structlog
import structlog.testing

from app.config import CONFIGS_DIR, AiConfig, Settings, load_settings
from app.logging import setup_logging
from app.pipeline.ai.client import (
    AiFatalError,
    AiRawResponse,
    AiResponseError,
    AiUnavailableError,
    DeepSeekClient,
)
from tests.config_helpers import base_section

API_KEY = "sk-deepseek-secret-key"
CFG = AiConfig(**{**base_section("ai"), "max_retries": 2, "max_concurrency": 2})
REQUEST = httpx2.Request("POST", "https://api.example/v1/chat/completions")
MESSAGES = [{"role": "system", "content": "Ты редактор"}, {"role": "user", "content": "Текст"}]


def _status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    return cls(f"HTTP {status}", response=httpx2.Response(status, request=REQUEST), body=None)


def _response(
    content: str | None = '{"title": "t"}',
    usage: tuple[int, int] | None = (120, 40),
    finish_reason: str | None = "stop",
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
        ],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1])
        if usage
        else None,
    )


class FakeCompletions:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0
        self.gate: asyncio.Event | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            outcome = self.outcomes.pop(0) if self.outcomes else _response()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.active -= 1


class FakeOpenAI:
    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.completions = FakeCompletions(outcomes or [])
        self.chat = SimpleNamespace(completions=self.completions)


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    env = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
        "TELEGRAM_BOT_TOKEN": "bot-token",
        "TELEGRAM_CHANNEL_ID": "-100",
        "TELEGRAM_ADMIN_IDS": "1",
        "TELETHON_API_ID": "1",
        "TELETHON_API_HASH": "hash",
        "DEEPSEEK_API_KEY": API_KEY,
        "DEEPSEEK_BASE_URL": "https://api.example/v1",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    configs = tmp_path / "configs"
    configs.mkdir()
    shutil.copy(CONFIGS_DIR / "base.yaml", configs / "base.yaml")
    return load_settings(configs, "dev")


def _client(settings: Settings, fake: FakeOpenAI, sleep: FakeSleep | None = None) -> DeepSeekClient:
    return DeepSeekClient(settings, CFG, fake, sleep=sleep or FakeSleep())


async def test_request_parameters_and_result(settings: Settings) -> None:
    fake = FakeOpenAI([_response('{"summary": "ok"}', (150, 60))])
    result = await _client(settings, fake).chat_json(MESSAGES)
    assert result == AiRawResponse(
        content='{"summary": "ok"}', prompt_tokens=150, completion_tokens=60
    )
    [call] = fake.completions.calls
    assert call["model"] == CFG.model
    assert call["messages"] == MESSAGES
    assert call["temperature"] == CFG.temperature
    assert call["response_format"] == {"type": "json_object"}
    assert call["timeout"] == CFG.timeout_s
    assert call["max_tokens"] == CFG.max_output_tokens


async def test_max_tokens_from_argument(settings: Settings) -> None:
    fake = FakeOpenAI([_response()])
    await _client(settings, fake).chat_json(MESSAGES, max_tokens=120)
    [call] = fake.completions.calls
    assert call["max_tokens"] == 120


async def test_truncated_response_is_bad_response(settings: Settings) -> None:
    """finish_reason = length: ответ обрезан, в канал он не попадёт."""
    fake = FakeOpenAI([_response('{"title": "обрез', (150, 800), finish_reason="length")])
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(AiResponseError, match="обрезан"),
    ):
        await _client(settings, fake).chat_json(MESSAGES)
    [entry] = [e for e in logs if e["event"] == "ai.truncated"]
    assert (entry["prompt_tokens"], entry["completion_tokens"]) == (150, 800)
    assert len(fake.completions.calls) == 1, "обрезанный ответ не повторяется внутри клиента"


@pytest.mark.parametrize("finish_reason", ["stop", None, "content_filter"])
async def test_other_finish_reasons_are_normal(
    settings: Settings, finish_reason: str | None
) -> None:
    fake = FakeOpenAI([_response(finish_reason=finish_reason)])
    assert (await _client(settings, fake).chat_json(MESSAGES)).content == '{"title": "t"}'


async def test_retry_on_429(settings: Settings) -> None:
    sleep = FakeSleep()
    fake = FakeOpenAI([_status_error(openai.RateLimitError, 429), _response()])
    result = await _client(settings, fake, sleep).chat_json(MESSAGES)
    assert result.prompt_tokens == 120
    assert len(fake.completions.calls) == 2
    assert len(sleep.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=REQUEST),
        openai.APITimeoutError(request=REQUEST),
        _status_error(openai.InternalServerError, 503),
    ],
)
async def test_retry_on_network_and_server_errors(settings: Settings, error: Exception) -> None:
    fake = FakeOpenAI([error, _response()])
    assert (await _client(settings, fake).chat_json(MESSAGES)).completion_tokens == 40


@pytest.mark.parametrize(
    ("cls", "status"),
    [
        (openai.AuthenticationError, 401),
        (openai.BadRequestError, 400),
        (openai.PermissionDeniedError, 403),
        (openai.NotFoundError, 404),
    ],
)
async def test_fatal_errors_without_retry(
    settings: Settings, cls: type[openai.APIStatusError], status: int
) -> None:
    fake = FakeOpenAI([_status_error(cls, status)])
    with pytest.raises(AiFatalError, match=str(status)):
        await _client(settings, fake).chat_json(MESSAGES)
    assert len(fake.completions.calls) == 1


async def test_retries_exhausted(settings: Settings) -> None:
    sleep = FakeSleep()
    errors = [_status_error(openai.RateLimitError, 429)] * (CFG.max_retries + 1)
    fake = FakeOpenAI(list(errors))
    with pytest.raises(AiUnavailableError):
        await _client(settings, fake, sleep).chat_json(MESSAGES)
    assert len(fake.completions.calls) == CFG.max_retries + 1
    assert len(sleep.calls) == CFG.max_retries


async def test_concurrency_limit(settings: Settings) -> None:
    fake = FakeOpenAI()
    fake.completions.gate = asyncio.Event()
    client = _client(settings, fake)
    tasks = [asyncio.create_task(client.chat_json(MESSAGES)) for _ in range(5)]
    for _ in range(10):
        await asyncio.sleep(0)
    assert fake.completions.active == CFG.max_concurrency
    fake.completions.gate.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    assert len(results) == 5
    assert fake.completions.max_active == CFG.max_concurrency


@pytest.mark.parametrize("content", [None, "", "   "])
async def test_empty_content(settings: Settings, content: str | None) -> None:
    fake = FakeOpenAI([_response(content)])
    with pytest.raises(AiResponseError, match="пустой"):
        await _client(settings, fake).chat_json(MESSAGES)


async def test_missing_usage_gives_zero_tokens(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_logging("INFO", json_output=True)
    try:
        fake = FakeOpenAI([_response(usage=None)])
        result = await _client(settings, fake).chat_json(MESSAGES)
    finally:
        structlog.reset_defaults()
    assert (result.prompt_tokens, result.completion_tokens) == (0, 0)
    assert "ai.usage_missing" in capsys.readouterr().out


async def test_real_openai_client_built_without_leaking_key(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_logging("INFO", json_output=True)
    try:
        client = DeepSeekClient(settings, CFG)
        inner: Any = client._client
        assert isinstance(inner, openai.AsyncOpenAI)
        assert str(inner.base_url).startswith("https://api.example/v1")
        assert inner.max_retries == 0
        await inner.close()
    finally:
        structlog.reset_defaults()
    assert API_KEY not in capsys.readouterr().out
    assert API_KEY not in repr(settings)
