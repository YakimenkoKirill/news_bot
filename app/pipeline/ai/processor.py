"""Обработка одной новости моделью: отбор по теме, пересказ, итоговый статус."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, TypeVar

import structlog

from app.config import AiConfig, ChannelProfile
from app.db.models import AiRequest, ProcessedPost
from app.domain.enums import ModerationMode, NewsStatus
from app.pipeline.ai.budget import cost
from app.pipeline.ai.client import AiRawResponse, AiResponseError, Message
from app.pipeline.ai.factcheck import FOREIGN_NOTE, is_foreign, unsupported_facts
from app.pipeline.ai.prompts import (
    LEGACY_PROMPT_VERSION,
    PROMPT_VERSION,
    build_legacy_messages,
    build_messages,
    build_screen_messages,
)
from app.pipeline.ai.schema import (
    AiResult,
    ScreenResult,
    SummaryResult,
    parse_ai_response,
    parse_screen_response,
    parse_summary_response,
)
from app.pipeline.queue_limit import AdmitOutcome, AdmitResult

log = structlog.get_logger(__name__)

REASON_NOT_RELEVANT = "not_relevant"
REASON_BAD_AI_RESPONSE = "bad_ai_response"
KIND_SUMMARY = "summary"
KIND_SCREEN = "screen"
ATTEMPTS = (1, 2)

_ResultT = TypeVar("_ResultT")


class ChatClient(Protocol):
    """Нужная процессору часть DeepSeekClient."""

    async def chat_json(
        self, messages: list[Message], *, max_tokens: int | None = None
    ) -> AiRawResponse:
        """Запрос к модели с ответом в JSON."""
        raise NotImplementedError


class BudgetCheck(Protocol):
    """Проверка дневного бюджета между запросами (BudgetGuard подходит структурно)."""

    async def check(self, now: datetime) -> Any:
        """Бросает BudgetExceeded, если бюджет исчерпан."""
        raise NotImplementedError


class ProcessableNews(Protocol):
    """Поля новости для обработки (NewsItem подходит структурно)."""

    id: int
    source_id: int
    title: str
    url: str
    clean_text: str | None
    raw_text: str
    language: str | None
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


@dataclass
class _Totals:
    """Расход новости по всем её запросам к модели."""

    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    spent: Decimal = field(default_factory=lambda: Decimal(0))

    def add(self, raw: AiRawResponse, spent: Decimal) -> None:
        """Добавляет токены и стоимость одного запроса."""
        self.prompt_tokens += raw.prompt_tokens
        self.cached_tokens += raw.cached_tokens
        self.completion_tokens += raw.completion_tokens
        self.spent += spent


class AiProcessor:
    """Отбирает новость по теме, пересказывает её и переводит в итоговый статус."""

    def __init__(
        self,
        client: ChatClient,
        profile: ChannelProfile,
        repos: AiRepos,
        cfg: AiConfig,
        *,
        budget: BudgetCheck | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._profile = profile
        self._repos = repos
        self._cfg = cfg
        self._budget = budget
        self._clock = clock or (lambda: datetime.now(UTC))

    async def process(self, news: ProcessableNews, source: ModeratedSource) -> NewsStatus:
        """Обрабатывает новость в статусе normalized.

        При ai.two_stage новость сначала проходит короткий отбор по теме
        (build_screen_messages, ai.screen_max_tokens): «не по теме» или оценка
        ниже ai.min_score — filtered, и пересказ не запрашивается. Прошедшую
        новость пересказывает второй запрос. При ai.two_stage = false оба решения
        даёт один запрос промпта v3. На каждый запрос пишется AiRequest, плохой
        ответ повторяется один раз, дважды плохой — failed с bad_ai_response.

        Args:
            news: Новость в статусе normalized.
            source: Источник новости (режим модерации).

        Returns:
            Итоговый статус новости.

        Raises:
            AiUnavailableError: API недоступен; новость остаётся normalized.
            AiFatalError: Запрос отклонён; новость остаётся normalized.
            BudgetExceeded: Бюджет исчерпан между отбором и пересказом; новость
                остаётся normalized, запрос отбора сохранён.
        """
        totals = _Totals()
        if not self._cfg.two_stage:
            return await self._process_single(news, source, totals)

        screen = await self._ask(
            news.id,
            build_screen_messages(news, self._profile, self._cfg.screen_input_chars),
            KIND_SCREEN,
            parse_screen_response,
            totals,
            max_tokens=self._cfg.screen_max_tokens,
        )
        if screen is None:
            return await self._fail(news.id, totals)
        if not self._is_relevant(screen):
            return await self._filter_out(news.id, screen)

        if self._budget is not None:
            await self._budget.check(self._clock())

        summary = await self._ask(
            news.id,
            build_messages(news, self._profile, self._cfg.max_input_chars),
            KIND_SUMMARY,
            lambda content: parse_summary_response(content, self._profile),
            totals,
        )
        if summary is None:
            return await self._fail(news.id, totals)
        return await self._store(news, source, summary, screen.score, PROMPT_VERSION, totals)

    async def _process_single(
        self, news: ProcessableNews, source: ModeratedSource, totals: _Totals
    ) -> NewsStatus:
        """Прежний путь: оценка и пересказ одним запросом (ai.two_stage = false)."""
        result = await self._ask(
            news.id,
            build_legacy_messages(news, self._profile, self._cfg.max_input_chars),
            KIND_SUMMARY,
            lambda content: parse_ai_response(content, self._profile),
            totals,
        )
        if result is None:
            return await self._fail(news.id, totals)
        return await self._store(
            news, source, result, result.score, LEGACY_PROMPT_VERSION, totals, relevance=result
        )

    async def _ask(
        self,
        news_id: int,
        messages: Sequence[Message],
        kind: str,
        parse: Callable[[str], _ResultT],
        totals: _Totals,
        *,
        max_tokens: int | None = None,
    ) -> _ResultT | None:
        """Делает запрос к модели, повторяя его один раз при плохом ответе.

        Args:
            news_id: Новость, из-за которой идёт запрос.
            messages: Сообщения промпта.
            kind: Вид запроса для ai_requests (screen или summary).
            parse: Разбор ответа; бросает AiResponseError на плохом ответе.
            totals: Накопитель токенов и стоимости новости.
            max_tokens: Предел длины ответа; None — ai.max_output_tokens.

        Returns:
            Разобранный ответ или None, если обе попытки дали плохой ответ.
        """
        for attempt in ATTEMPTS:
            try:
                raw = await self._client.chat_json(list(messages), max_tokens=max_tokens)
            except AiResponseError as exc:
                log.warning("ai.bad_response", kind=kind, attempt=attempt, error=str(exc))
                await self._record(news_id, AiRawResponse("", 0, 0), kind, ok=False)
                continue
            try:
                result = parse(raw.content)
            except AiResponseError as exc:
                log.warning("ai.bad_response", kind=kind, attempt=attempt, error=str(exc))
                totals.add(raw, await self._record(news_id, raw, kind, ok=False))
                continue
            totals.add(raw, await self._record(news_id, raw, kind, ok=True))
            return result
        return None

    def _is_relevant(self, result: ScreenResult | AiResult) -> bool:
        return result.relevant and result.score >= self._cfg.min_score

    async def _filter_out(self, news_id: int, result: ScreenResult | AiResult) -> NewsStatus:
        await self._repos.news.set_status(
            news_id, NewsStatus.NORMALIZED, NewsStatus.FILTERED, REASON_NOT_RELEVANT
        )
        log.info("ai.not_relevant", relevant=result.relevant, score=result.score)
        return NewsStatus.FILTERED

    async def _fail(self, news_id: int, totals: _Totals) -> NewsStatus:
        log.error(
            "ai.failed",
            prompt_tokens=totals.prompt_tokens,
            cached_tokens=totals.cached_tokens,
            completion_tokens=totals.completion_tokens,
            spent=str(totals.spent),
        )
        await self._repos.news.set_status(
            news_id, NewsStatus.NORMALIZED, NewsStatus.FAILED, REASON_BAD_AI_RESPONSE
        )
        return NewsStatus.FAILED

    async def _store(
        self,
        news: ProcessableNews,
        source: ModeratedSource,
        summary: SummaryResult,
        score: float,
        prompt_version: str,
        totals: _Totals,
        relevance: AiResult | None = None,
    ) -> NewsStatus:
        """Сохраняет пересказ с проверкой фактов и переводит новость в итоговый статус.

        Пересказ сохраняется и для новости, которую отсеяла оценка в том же ответе
        (ai.two_stage = false): он уже оплачен, и его видно в отчёте. У перевода
        проверяются только числа, и в замечание добавляется пометка об этом.
        """
        source_text = f"{news.title}\n{news.clean_text or news.raw_text}"
        channel_lang = self._profile.language
        foreign = is_foreign(news.language, channel_lang)
        facts = unsupported_facts(
            source_text,
            f"{summary.title}\n{summary.summary}",
            source_lang=news.language,
            channel_lang=channel_lang,
        )
        notes: list[str] = []
        if facts:
            notes.append(f"Не найдены в источнике: {', '.join(facts)}")
        if foreign:
            notes.append(FOREIGN_NOTE.format(lang=news.language))
        review_note = "; ".join(notes) if notes else None
        await self._repos.posts.add(
            ProcessedPost(
                news_item_id=news.id,
                ai_title=summary.title,
                ai_summary=summary.summary,
                hashtags=summary.hashtags,
                category=summary.category or None,
                relevance_score=score,
                prompt_version=prompt_version,
                prompt_tokens=totals.prompt_tokens,
                completion_tokens=totals.completion_tokens,
                cost=totals.spent,
                review_note=review_note,
            )
        )
        if relevance is not None and not self._is_relevant(relevance):
            return await self._filter_out(news.id, relevance)
        return await self._finish(
            news.id,
            news.source_id,
            review_note,
            source,
            flagged=bool(facts),
            foreign=foreign,
        )

    async def _record(self, news_id: int, raw: AiRawResponse, kind: str, *, ok: bool) -> Decimal:
        """Записывает один запрос к модели и возвращает его стоимость.

        Args:
            news_id: Новость, из-за которой пошёл запрос.
            raw: Ответ модели; у неудачной попытки без ответа — нули.
            kind: Вид запроса (screen или summary).
            ok: Разобран ли ответ.

        Returns:
            Стоимость запроса в долларах.
        """
        spent = cost(raw.prompt_tokens, raw.cached_tokens, raw.completion_tokens, self._cfg)
        await self._repos.requests.add(
            AiRequest(
                news_item_id=news_id,
                kind=kind,
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
        review_note: str | None,
        source: ModeratedSource,
        *,
        flagged: bool = False,
        foreign: bool = False,
    ) -> NewsStatus:
        """Доводит новость до итогового статуса.

        На модерацию уходят посты с неподтверждёнными фактами, посты источников
        с режимом review и — при ai.review_foreign — переводы: качество перевода
        сначала смотрит человек. Пометка о переводе сама по себе на модерацию не
        отправляет: при review_foreign = false перевод идёт по режиму источника.

        Args:
            news_id: Новость.
            source_id: Её источник.
            review_note: Замечание для администратора (может быть и у approved).
            source: Источник (режим модерации).
            flagged: Есть ли неподтверждённые факты.
            foreign: Пересказ — перевод с другого языка.

        Returns:
            Итоговый статус новости.
        """
        news_repo = self._repos.news
        await news_repo.set_status(news_id, NewsStatus.NORMALIZED, NewsStatus.PROCESSED)
        to_review = (
            flagged
            or source.moderation_mode is ModerationMode.REVIEW
            or (foreign and self._cfg.review_foreign)
        )
        if to_review:
            await news_repo.set_status(news_id, NewsStatus.PROCESSED, NewsStatus.PENDING_REVIEW)
            log.info("ai.pending_review", review_note=review_note, foreign=foreign)
            return NewsStatus.PENDING_REVIEW
        if self._repos.admit is None:
            await news_repo.set_status(news_id, NewsStatus.PROCESSED, NewsStatus.APPROVED)
            return NewsStatus.APPROVED
        admitted = await self._repos.admit(news_id, source_id)
        if admitted.outcome is AdmitOutcome.REJECTED:
            log.info("ai.queue_limit")
            return NewsStatus.FILTERED
        return NewsStatus.APPROVED
