"""Обработка одной новости моделью: пересказ, проверка фактов, итоговый статус."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

import structlog

from app.config import AiConfig, ChannelProfile
from app.db.models import AiRequest, ProcessedPost
from app.domain.enums import ModerationMode, NewsStatus
from app.pipeline.ai.budget import cost
from app.pipeline.ai.client import AiRawResponse, AiResponseError, Message
from app.pipeline.ai.factcheck import unsupported_facts
from app.pipeline.ai.prompts import PROMPT_VERSION, build_messages
from app.pipeline.ai.schema import AiResult, parse_ai_response
from app.pipeline.queue_limit import AdmitOutcome, AdmitResult

log = structlog.get_logger(__name__)

REASON_NOT_RELEVANT = "not_relevant"
REASON_BAD_AI_RESPONSE = "bad_ai_response"
KIND_SUMMARY = "summary"


class ChatClient(Protocol):
    """Нужная процессору часть DeepSeekClient."""

    async def chat_json(self, messages: list[Message]) -> AiRawResponse:
        """Запрос к модели с ответом в JSON."""
        raise NotImplementedError


class ProcessableNews(Protocol):
    """Поля новости для обработки (NewsItem подходит структурно)."""

    id: int
    source_id: int
    title: str
    url: str
    clean_text: str | None
    raw_text: str
    published_at: datetime | None


class ModeratedSource(Protocol):
    """Источник с режимом модерации (Source подходит структурно)."""

    moderation_mode: ModerationMode


class StatusRepo(Protocol):
    """Нужная процессору часть NewsRepository."""

    async def set_status(
        self,
        news_id: int,
        expected: NewsStatus,
        new: NewsStatus,
        reason: str | None = None,
    ) -> None:
        """Смена статуса через машину состояний."""
        raise NotImplementedError


class PostRepo(Protocol):
    """Нужная процессору часть ProcessedPostRepository."""

    async def add(self, post: ProcessedPost) -> Any:
        """Сохраняет пересказ."""
        raise NotImplementedError


class AiRequestRepo(Protocol):
    """Нужная процессору часть AiRequestRepository."""

    async def add(self, request: AiRequest) -> Any:
        """Сохраняет запрос к модели."""
        raise NotImplementedError


@dataclass(frozen=True)
class AiRepos:
    """Репозитории одной сессии БД.

    Attributes:
        news, posts, requests: Репозитории.
        admit: Постановка в очередь с учётом лимита источника (news_id, source_id).
            None — прежний прямой переход в approved.
    """

    news: StatusRepo
    posts: PostRepo
    requests: AiRequestRepo
    admit: Callable[[int, int], Awaitable[AdmitResult]] | None = None


class AiProcessor:
    """Пересказывает новость моделью и переводит её в итоговый статус."""

    def __init__(
        self, client: ChatClient, profile: ChannelProfile, repos: AiRepos, cfg: AiConfig
    ) -> None:
        self._client = client
        self._profile = profile
        self._repos = repos
        self._cfg = cfg

    async def process(self, news: ProcessableNews, source: ModeratedSource) -> NewsStatus:
        """Обрабатывает новость в статусе normalized.

        build_messages -> chat_json -> parse_ai_response (при плохом ответе один
        повтор) -> unsupported_facts -> ProcessedPost. Итог: filtered (не по теме или
        score < min_score), pending_review (неподтверждённые факты или режим review),
        approved (режим auto), failed (плохой ответ дважды).

        Returns:
            Итоговый статус новости.

        Raises:
            AiUnavailableError: API недоступен; новость остаётся normalized.
            AiFatalError: Запрос отклонён; новость остаётся normalized.
        """
        messages = build_messages(news, self._profile, self._cfg.max_input_chars)
        prompt_tokens = completion_tokens = cached_tokens = 0
        spent = Decimal(0)
        result: AiResult | None = None
        for attempt in (1, 2):
            try:
                raw = await self._client.chat_json(messages)
            except AiResponseError as exc:
                log.warning("ai.bad_response", attempt=attempt, error=str(exc))
                await self._record(news.id, AiRawResponse("", 0, 0), ok=False)
                continue
            prompt_tokens += raw.prompt_tokens
            completion_tokens += raw.completion_tokens
            cached_tokens += raw.cached_tokens
            try:
                result = parse_ai_response(raw.content, self._profile)
            except AiResponseError as exc:
                log.warning("ai.bad_response", attempt=attempt, error=str(exc))
                spent += await self._record(news.id, raw, ok=False)
                continue
            spent += await self._record(news.id, raw, ok=True)
            break

        if result is None:
            log.error(
                "ai.failed",
                prompt_tokens=prompt_tokens,
                cached_tokens=cached_tokens,
                completion_tokens=completion_tokens,
                spent=str(spent),
            )
            await self._repos.news.set_status(
                news.id, NewsStatus.NORMALIZED, NewsStatus.FAILED, REASON_BAD_AI_RESPONSE
            )
            return NewsStatus.FAILED

        source_text = f"{news.title}\n{news.clean_text or news.raw_text}"
        facts = unsupported_facts(source_text, f"{result.title}\n{result.summary}")
        review_note = f"Не найдены в источнике: {', '.join(facts)}" if facts else None
        await self._repos.posts.add(
            ProcessedPost(
                news_item_id=news.id,
                ai_title=result.title,
                ai_summary=result.summary,
                hashtags=result.hashtags,
                category=result.category or None,
                relevance_score=result.score,
                prompt_version=PROMPT_VERSION,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=spent,
                review_note=review_note,
            )
        )
        return await self._finish(news.id, news.source_id, result, review_note, source)

    async def _record(self, news_id: int, raw: AiRawResponse, *, ok: bool) -> Decimal:
        """Записывает один запрос к модели и возвращает его стоимость.

        Args:
            news_id: Новость, из-за которой пошёл запрос.
            raw: Ответ модели; у неудачной попытки без ответа — нули.
            ok: Разобран ли ответ.

        Returns:
            Стоимость запроса в долларах.
        """
        spent = cost(raw.prompt_tokens, raw.cached_tokens, raw.completion_tokens, self._cfg)
        await self._repos.requests.add(
            AiRequest(
                news_item_id=news_id,
                kind=KIND_SUMMARY,
                model=self._cfg.model,
                prompt_tokens=raw.prompt_tokens,
                cached_tokens=raw.cached_tokens,
                completion_tokens=raw.completion_tokens,
                cost=spent,
                ok=ok,
            )
        )
        return spent

    async def _finish(
        self,
        news_id: int,
        source_id: int,
        result: AiResult,
        review_note: str | None,
        source: ModeratedSource,
    ) -> NewsStatus:
        news_repo = self._repos.news
        if not result.relevant or result.score < self._cfg.min_score:
            await news_repo.set_status(
                news_id, NewsStatus.NORMALIZED, NewsStatus.FILTERED, REASON_NOT_RELEVANT
            )
            log.info("ai.not_relevant", relevant=result.relevant, score=result.score)
            return NewsStatus.FILTERED

        await news_repo.set_status(news_id, NewsStatus.NORMALIZED, NewsStatus.PROCESSED)
        if review_note is not None or source.moderation_mode is ModerationMode.REVIEW:
            await news_repo.set_status(news_id, NewsStatus.PROCESSED, NewsStatus.PENDING_REVIEW)
            log.info("ai.pending_review", review_note=review_note)
            return NewsStatus.PENDING_REVIEW
        if self._repos.admit is None:
            await news_repo.set_status(news_id, NewsStatus.PROCESSED, NewsStatus.APPROVED)
            return NewsStatus.APPROVED
        admitted = await self._repos.admit(news_id, source_id)
        if admitted.outcome is AdmitOutcome.REJECTED:
            log.info("ai.queue_limit", score=result.score)
            return NewsStatus.FILTERED
        return NewsStatus.APPROVED
