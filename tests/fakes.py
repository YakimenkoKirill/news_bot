"""Фейки внешних систем для сквозных тестов: сборщик, DeepSeek, Telegram-бот, алерты."""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import BaseCollector, RawItem
from app.db.models import ProcessedPost, Source
from app.db.repositories.ai_requests import AiRequestRepository
from app.domain.alerts import AlertEvent
from app.pipeline.ai.client import AiRawResponse, Message
from app.publisher.media import ImageCheck

NOT_RELEVANT_MARKER = "нерелевантн"
SCREEN_MARKER = "Пересказывать новость не нужно"


class FakeCollector(BaseCollector):
    """Сборщик, который отдаёт заранее заданные новости по url источника."""

    def __init__(self, items_by_url: dict[str, list[dict[str, Any]]]) -> None:
        self.items_by_url = items_by_url

    async def fetch(self, source: Source) -> list[RawItem]:
        items = self.items_by_url.get(source.url, [])
        return [RawItem(source_id=source.id, **fields) for fields in items]


@dataclass
class FakeDeepSeek:
    """Модель: отвечает корректным JSON; новость с «нерелевантн» в заголовке — не по теме.

    Отличает запрос отбора от запроса пересказа по системному сообщению, поэтому
    подходит и для ai.two_stage = true, и для прежнего одного запроса.
    """

    prompt_tokens: int = 1000
    completion_tokens: int = 200
    screen_prompt_tokens: int = 300
    screen_completion_tokens: int = 20
    calls: list[list[Message]] = field(default_factory=list)

    async def chat_json(
        self, messages: list[Message], *, max_tokens: int | None = None
    ) -> AiRawResponse:
        self.calls.append(messages)
        user = messages[-1]["content"]
        title_line = next(line for line in user.splitlines() if line.startswith("Заголовок:"))
        relevant = NOT_RELEVANT_MARKER not in title_line.lower()
        score = 0.9 if relevant else 0.1
        if SCREEN_MARKER in messages[0]["content"]:
            payload: dict[str, Any] = {"relevant": relevant, "score": score}
            return AiRawResponse(
                json.dumps(payload, ensure_ascii=False),
                self.screen_prompt_tokens,
                self.screen_completion_tokens,
            )
        payload = {
            "relevant": relevant,
            "score": score,
            "title": "Пересказ главной новости",
            "summary": "Коротко о том, что произошло, без новых фактов.",
            "hashtags": ["новости"],
            "category": "главное",
        }
        return AiRawResponse(
            json.dumps(payload, ensure_ascii=False), self.prompt_tokens, self.completion_tokens
        )


class FakeTelegramBot:
    """Бот Telegram: запоминает отправленные посты и выдаёт растущие message_id."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def send_photo(self, **kwargs: Any) -> SimpleNamespace:
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1000 + len(self.sent))


@dataclass
class RecordingSink:
    """Получатель алертов, который их запоминает."""

    events: list[AlertEvent] = field(default_factory=list)

    async def emit(self, event: AlertEvent) -> None:
        self.events.append(event)


@dataclass
class RecordingNotifier:
    """ReviewNotifier, который запоминает посты на модерации."""

    posts: list[ProcessedPost] = field(default_factory=list)

    async def notify(self, post: ProcessedPost) -> None:
        self.posts.append(post)


class SessionCost:
    """Расход на модель для BudgetGuard: каждый запрос в своей сессии."""

    def __init__(self, scope: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self.scope = scope

    async def cost_since(self, dt: datetime) -> Decimal:
        async with self.scope() as session:
            return await AiRequestRepository(session).cost_since(dt)


async def no_sleep(_: float) -> None:
    return None


async def image_never_ok(url: str | None) -> ImageCheck:
    """Проверка картинки без сети: пост уходит текстом."""
    return ImageCheck(ok=False, reason="no_image")
