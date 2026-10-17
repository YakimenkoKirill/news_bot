import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import structlog

from app.config import AiConfig, ChannelProfile
from app.db.models import AiRequest, ProcessedPost
from app.domain.enums import ModerationMode, NewsStatus
from app.domain.status import ensure_transition
from app.pipeline.ai.budget import BudgetExceeded, cost
from app.pipeline.ai.client import (
    AiFatalError,
    AiRawResponse,
    AiResponseError,
    AiUnavailableError,
    Message,
)
from app.pipeline.ai.processor import AiProcessor, AiRepos
from app.pipeline.ai.prompts import LEGACY_PROMPT_VERSION, PROMPT_VERSION
from app.pipeline.queue_limit import AdmitOutcome, AdmitResult
from tests.config_helpers import base_section

CFG = AiConfig(**{**base_section("ai"), "min_score": 0.5})
LEGACY_CFG = CFG.model_copy(update={"two_stage": False})
PROFILE = ChannelProfile(**{**base_section("channel"), "hashtags_max": 2})
SOURCE_TEXT = (
    "Компания Nvidia представила новый процессор. По данным компании, он на 30% быстрее "
    "предыдущей модели и поступит в продажу в 2027 году."
)


@dataclass
class News:
    id: int = 1
    source_id: int = 7
    title: str = "Nvidia представила процессор"
    url: str = "https://news.example/1"
    clean_text: str | None = SOURCE_TEXT
    raw_text: str = SOURCE_TEXT
    language: str | None = "ru"
    published_at: datetime | None = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


@dataclass
class Source:
    moderation_mode: ModerationMode = ModerationMode.AUTO


@dataclass
class FakeNewsRepo:
    status: NewsStatus = NewsStatus.NORMALIZED
    reason: str | None = None
    history: list[NewsStatus] = field(default_factory=list)

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        assert self.status == expected
        ensure_transition(expected, new)
        self.status = new
        self.history.append(new)
        if reason is not None:
            self.reason = reason


@dataclass
class FakePostRepo:
    posts: list[ProcessedPost] = field(default_factory=list)

    async def add(self, post: ProcessedPost) -> ProcessedPost:
        self.posts.append(post)
        return post


@dataclass
class FakeRequestRepo:
    requests: list[AiRequest] = field(default_factory=list)

    async def add(self, request: AiRequest) -> AiRequest:
        self.requests.append(request)
        return request


class FakeClient:
    def __init__(self, outcomes: list[AiRawResponse | Exception]) -> None:
        self.outcomes: list[AiRawResponse | Exception] = list(outcomes)
        self.calls: list[list[Message]] = []
        self.limits: list[int | None] = []

    async def chat_json(
        self, messages: list[Message], *, max_tokens: int | None = None
    ) -> AiRawResponse:
        self.calls.append(messages)
        self.limits.append(max_tokens)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _answer(**overrides: Any) -> AiRawResponse:
    """Ответ промпта v3: оценка и пересказ вместе (ai.two_stage = false)."""
    data: dict[str, Any] = {
        "relevant": True,
        "score": 0.8,
        "title": "Nvidia показала новый процессор",
        "summary": "Nvidia представила процессор, который на 30% быстрее прошлой модели.",
        "hashtags": ["железо"],
        "category": "технологии",
    }
    data.update(overrides)
    return AiRawResponse(json.dumps(data, ensure_ascii=False), 1000, 200)


def _screen(relevant: bool = True, score: float = 0.8, **overrides: Any) -> AiRawResponse:
    """Ответ отбора: только relevant и score."""
    data: dict[str, Any] = {"relevant": relevant, "score": score}
    data.update(overrides)
    return AiRawResponse(json.dumps(data, ensure_ascii=False), 300, 20)


def _summary(**overrides: Any) -> AiRawResponse:
    """Ответ пересказа промпта v4: без relevant и score."""
    data: dict[str, Any] = {
        "title": "Nvidia показала новый процессор",
        "summary": "Nvidia представила процессор, который на 30% быстрее прошлой модели.",
        "hashtags": ["железо"],
        "category": "технологии",
    }
    data.update(overrides)
    return AiRawResponse(json.dumps(data, ensure_ascii=False), 1000, 200)


async def _run(
    outcomes: list[Any], mode: ModerationMode = ModerationMode.AUTO
) -> tuple[NewsStatus, FakeNewsRepo, FakePostRepo, FakeClient]:
    news_repo, post_repo, client = FakeNewsRepo(), FakePostRepo(), FakeClient(outcomes)
    processor = AiProcessor(client, PROFILE, _repos(news_repo, post_repo), LEGACY_CFG)
    status = await processor.process(News(), Source(mode))
    return status, news_repo, post_repo, client


def _repos(
    news_repo: FakeNewsRepo,
    post_repo: FakePostRepo,
    requests: FakeRequestRepo | None = None,
    admit: Any = None,
) -> AiRepos:
    return AiRepos(
        news=news_repo, posts=post_repo, requests=requests or FakeRequestRepo(), admit=admit
    )


async def _run_two_stage(
    outcomes: list[Any],
    mode: ModerationMode = ModerationMode.AUTO,
    budget: Any = None,
) -> tuple[NewsStatus, FakeNewsRepo, FakePostRepo, FakeRequestRepo, FakeClient]:
    news_repo, post_repo = FakeNewsRepo(), FakePostRepo()
    requests, client = FakeRequestRepo(), FakeClient(outcomes)
    processor = AiProcessor(
        client, PROFILE, _repos(news_repo, post_repo, requests), CFG, budget=budget
    )
    status = await processor.process(News(), Source(mode))
    return status, news_repo, post_repo, requests, client


async def test_two_stage_not_relevant_costs_one_request() -> None:
    """Новость не по теме: пересказ не запрашивается и пост не создаётся."""
    status, news, posts, requests, client = await _run_two_stage([_screen(relevant=False)])
    assert status is NewsStatus.FILTERED
    assert news.reason == "not_relevant"
    assert posts.posts == []
    assert len(client.calls) == 1
    assert client.limits == [CFG.screen_max_tokens]
    [request] = requests.requests
    assert (request.kind, request.ok) == ("screen", True)


async def test_two_stage_low_score_costs_one_request() -> None:
    status, news, posts, _, client = await _run_two_stage([_screen(score=0.49)])
    assert status is NewsStatus.FILTERED
    assert news.reason == "not_relevant"
    assert posts.posts == []
    assert len(client.calls) == 1


async def test_two_stage_score_at_threshold_passes() -> None:
    status, _, posts, _, client = await _run_two_stage([_screen(score=CFG.min_score), _summary()])
    assert status is NewsStatus.APPROVED
    assert posts.posts[0].relevance_score == CFG.min_score
    assert len(client.calls) == 2


async def test_two_stage_relevant_makes_two_requests() -> None:
    status, news, posts, requests, client = await _run_two_stage([_screen(score=0.9), _summary()])
    assert status is NewsStatus.APPROVED
    assert news.history == [NewsStatus.PROCESSED, NewsStatus.APPROVED]
    [post] = posts.posts
    assert post.prompt_version == PROMPT_VERSION
    assert post.relevance_score == 0.9, "оценка берётся из отбора"
    assert post.ai_title == "Nvidia показала новый процессор"
    assert (post.prompt_tokens, post.completion_tokens) == (1300, 220)
    assert post.cost == cost(300, 0, 20, CFG) + cost(1000, 0, 200, CFG)
    assert [r.kind for r in requests.requests] == ["screen", "summary"]
    assert client.limits == [CFG.screen_max_tokens, None]


async def test_two_stage_screen_prompt_has_no_summary_rules() -> None:
    _, _, _, _, client = await _run_two_stage([_screen(), _summary()])
    screen_messages, summary_messages = client.calls
    assert "Пересказывать новость не нужно" in screen_messages[0]["content"]
    assert "summary" not in screen_messages[0]["content"]
    assert "Дата публикации" not in screen_messages[1]["content"]
    assert '"relevant"' not in summary_messages[0]["content"]


async def test_two_stage_bad_screen_response_retried() -> None:
    status, _, posts, requests, client = await _run_two_stage(
        [AiRawResponse("не json", 300, 20), _screen(), _summary()]
    )
    assert status is NewsStatus.APPROVED
    assert len(client.calls) == 3
    assert [(r.kind, r.ok) for r in requests.requests] == [
        ("screen", False),
        ("screen", True),
        ("summary", True),
    ]
    assert posts.posts[0].cost == sum(r.cost for r in requests.requests)


async def test_two_stage_two_bad_screen_responses_fail() -> None:
    status, news, posts, requests, client = await _run_two_stage(
        [AiRawResponse("не json", 300, 20), AiRawResponse("{}", 300, 20)]
    )
    assert status is NewsStatus.FAILED
    assert news.reason == "bad_ai_response"
    assert posts.posts == []
    assert len(client.calls) == 2
    assert [r.kind for r in requests.requests] == ["screen", "screen"]
    assert all(r.ok is False for r in requests.requests)


async def test_two_stage_screen_response_with_extra_keys_is_bad() -> None:
    """Отбор не должен возвращать пересказ: лишние ключи — плохой ответ."""
    status, _, _, requests, _ = await _run_two_stage([_answer(), _answer()])
    assert status is NewsStatus.FAILED
    assert [r.ok for r in requests.requests] == [False, False]


async def test_two_stage_bad_summary_response_retried() -> None:
    status, _, posts, requests, client = await _run_two_stage(
        [_screen(), AiRawResponse("{}", 1000, 200), _summary()]
    )
    assert status is NewsStatus.APPROVED
    assert len(client.calls) == 3
    assert [(r.kind, r.ok) for r in requests.requests] == [
        ("screen", True),
        ("summary", False),
        ("summary", True),
    ]
    assert posts.posts[0].cost == sum(r.cost for r in requests.requests)


async def test_two_stage_two_bad_summary_responses_fail() -> None:
    """Отбор прошёл, пересказ дважды плохой: оба запроса оплачены и записаны."""
    status, news, posts, requests, _ = await _run_two_stage(
        [_screen(), AiRawResponse("{}", 1000, 200), AiRawResponse("не json", 1000, 200)]
    )
    assert status is NewsStatus.FAILED
    assert news.reason == "bad_ai_response"
    assert posts.posts == []
    assert [(r.kind, r.ok) for r in requests.requests] == [
        ("screen", True),
        ("summary", False),
        ("summary", False),
    ]


async def test_two_stage_review_mode_and_facts() -> None:
    status, news, posts, _, _ = await _run_two_stage([_screen(), _summary()], ModerationMode.REVIEW)
    assert status is NewsStatus.PENDING_REVIEW
    assert news.history == [NewsStatus.PROCESSED, NewsStatus.PENDING_REVIEW]

    invented = _summary(summary="Процессор Nvidia на 45% быстрее, сообщил Дженсен Хуанг.")
    status, _, posts, _, _ = await _run_two_stage([_screen(), invented])
    assert status is NewsStatus.PENDING_REVIEW
    assert posts.posts[0].review_note is not None


async def test_budget_exceeded_between_stages_keeps_screen_request() -> None:
    """Бюджет кончился после отбора: пересказ не запрашивается, запись отбора цела."""

    class Exhausted:
        async def check(self, now: datetime) -> None:
            raise BudgetExceeded(Decimal("1"), Decimal("1"))

    news_repo, post_repo = FakeNewsRepo(), FakePostRepo()
    requests, client = FakeRequestRepo(), FakeClient([_screen(), _summary()])
    processor = AiProcessor(
        client, PROFILE, _repos(news_repo, post_repo, requests), CFG, budget=Exhausted()
    )
    with pytest.raises(BudgetExceeded):
        await processor.process(News(), Source())
    assert len(client.calls) == 1
    assert [(r.kind, r.ok) for r in requests.requests] == [("screen", True)]
    assert news_repo.status is NewsStatus.NORMALIZED
    assert post_repo.posts == []


async def test_budget_checked_only_between_stages() -> None:
    """Пройденный бюджет не мешает: обработка идёт как обычно."""

    class Ok:
        def __init__(self) -> None:
            self.calls = 0

        async def check(self, now: datetime) -> None:
            self.calls += 1

    budget = Ok()
    status, _, _, _, _ = await _run_two_stage([_screen(), _summary()], budget=budget)
    assert status is NewsStatus.APPROVED
    assert budget.calls == 1


async def test_auto_mode_approved() -> None:
    status, news, posts, _ = await _run([_answer()])
    assert status is NewsStatus.APPROVED
    assert news.history == [NewsStatus.PROCESSED, NewsStatus.APPROVED]
    [post] = posts.posts
    assert post.news_item_id == 1
    assert post.ai_title == "Nvidia показала новый процессор"
    assert post.hashtags == ["#железо"]
    assert post.prompt_version == LEGACY_PROMPT_VERSION
    assert (post.prompt_tokens, post.completion_tokens) == (1000, 200)
    assert post.cost == cost(1000, 0, 200, CFG), "стоимость считается по ценам из конфигурации"
    assert post.review_note is None


async def test_review_mode_pending() -> None:
    status, news, posts, _ = await _run([_answer()], ModerationMode.REVIEW)
    assert status is NewsStatus.PENDING_REVIEW
    assert news.history == [NewsStatus.PROCESSED, NewsStatus.PENDING_REVIEW]
    assert posts.posts[0].review_note is None


async def test_not_relevant_filtered() -> None:
    status, news, posts, _ = await _run([_answer(relevant=False)])
    assert status is NewsStatus.FILTERED
    assert news.reason == "not_relevant"
    assert news.history == [NewsStatus.FILTERED]
    assert len(posts.posts) == 1


async def test_low_score_filtered() -> None:
    status, news, _, _ = await _run([_answer(score=0.49)])
    assert status is NewsStatus.FILTERED
    assert news.reason == "not_relevant"
    status_at_threshold, _, _, _ = await _run([_answer(score=CFG.min_score)])
    assert status_at_threshold is NewsStatus.APPROVED


async def test_invented_fact_goes_to_review_even_in_auto() -> None:
    answer = _answer(summary="Процессор Nvidia на 45% быстрее, сообщил Дженсен Хуанг.")
    status, news, posts, _ = await _run([answer])
    assert status is NewsStatus.PENDING_REVIEW
    assert news.history == [NewsStatus.PROCESSED, NewsStatus.PENDING_REVIEW]
    note = posts.posts[0].review_note
    assert note is not None
    assert "45%" in note
    assert "Дженсен" in note


async def test_retry_after_bad_response() -> None:
    bad = AiRawResponse("это не json", 900, 50)
    status, _, posts, client = await _run([bad, _answer()])
    assert status is NewsStatus.APPROVED
    assert len(client.calls) == 2
    post = posts.posts[0]
    assert (post.prompt_tokens, post.completion_tokens) == (1900, 250)


async def test_retry_after_empty_response() -> None:
    status, _, _, client = await _run([AiResponseError("пустой"), _answer()])
    assert status is NewsStatus.APPROVED
    assert len(client.calls) == 2


async def test_truncated_response_retried_then_post_created() -> None:
    """Обрезанный ответ ведёт себя как плохой: один повтор, и пост создаётся."""
    status, _, posts, client = await _run(
        [AiResponseError("ответ модели обрезан по max_tokens"), _answer()]
    )
    assert status is NewsStatus.APPROVED
    assert len(client.calls) == 2
    assert len(posts.posts) == 1


async def test_two_truncated_responses_fail() -> None:
    truncated = AiResponseError("ответ модели обрезан по max_tokens")
    status, news, posts, client = await _run([truncated, truncated])
    assert status is NewsStatus.FAILED
    assert news.reason == "bad_ai_response"
    assert posts.posts == []
    assert len(client.calls) == 2


async def test_second_bad_response_fails() -> None:
    status, news, posts, client = await _run(
        [AiRawResponse("{}", 10, 5), AiResponseError("пустой")]
    )
    assert status is NewsStatus.FAILED
    assert news.reason == "bad_ai_response"
    assert news.history == [NewsStatus.FAILED]
    assert posts.posts == []
    assert len(client.calls) == 2


async def _run_foreign(
    cfg: AiConfig,
    language: str = "en",
    summary: str = "Nvidia представила процессор, который на 30% быстрее прошлой модели.",
    mode: ModerationMode = ModerationMode.AUTO,
) -> tuple[NewsStatus, FakePostRepo]:
    news_repo, post_repo = FakeNewsRepo(), FakePostRepo()
    outcomes: list[AiRawResponse | Exception] = (
        [_screen(), _summary(summary=summary)] if cfg.two_stage else [_answer(summary=summary)]
    )
    processor = AiProcessor(FakeClient(outcomes), PROFILE, _repos(news_repo, post_repo), cfg)
    status = await processor.process(News(language=language), Source(mode))
    return status, post_repo


async def test_foreign_news_goes_to_review() -> None:
    """Перевод сначала смотрит человек, даже если источник работает автоматически."""
    status, posts = await _run_foreign(CFG)
    assert status is NewsStatus.PENDING_REVIEW
    assert posts.posts[0].review_note is not None
    assert "перевод из en" in posts.posts[0].review_note


async def test_foreign_news_without_review_follows_source_mode() -> None:
    """review_foreign = false: перевод идёт по режиму источника, пометка остаётся."""
    cfg = CFG.model_copy(update={"review_foreign": False})
    status, posts = await _run_foreign(cfg)
    assert status is NewsStatus.APPROVED
    assert posts.posts[0].review_note == "перевод из en: имена не проверены автоматически"


async def test_foreign_news_note_lists_numbers_too() -> None:
    cfg = CFG.model_copy(update={"review_foreign": False})
    status, posts = await _run_foreign(cfg, summary="Процессор быстрее на 45%.")
    assert status is NewsStatus.PENDING_REVIEW
    note = posts.posts[0].review_note
    assert note is not None
    assert "45%" in note
    assert "перевод из en" in note


async def test_foreign_names_not_flagged() -> None:
    """Имена в переводе не сверяются по началу слова: иначе одни ложные замечания."""
    cfg = CFG.model_copy(update={"review_foreign": False})
    status, posts = await _run_foreign(
        cfg, summary="Дженсен Хуанг показал процессор, который на 30% быстрее прошлой модели."
    )
    assert status is NewsStatus.APPROVED
    note = posts.posts[0].review_note
    assert note is not None
    assert "Не найдены в источнике" not in note


async def test_russian_news_behaviour_unchanged() -> None:
    status, posts = await _run_foreign(CFG, language="ru")
    assert status is NewsStatus.APPROVED
    assert posts.posts[0].review_note is None


async def test_unknown_language_treated_as_channel_language() -> None:
    status, _ = await _run_foreign(CFG, language="")
    assert status is NewsStatus.APPROVED


async def test_foreign_news_in_single_request_mode_goes_to_review() -> None:
    status, posts = await _run_foreign(LEGACY_CFG)
    assert status is NewsStatus.PENDING_REVIEW
    assert posts.posts[0].review_note is not None


async def _run_with_requests(
    outcomes: list[Any],
) -> tuple[NewsStatus, FakeRequestRepo, FakePostRepo]:
    news_repo, post_repo, requests = FakeNewsRepo(), FakePostRepo(), FakeRequestRepo()
    processor = AiProcessor(
        FakeClient(outcomes), PROFILE, _repos(news_repo, post_repo, requests), LEGACY_CFG
    )
    status = await processor.process(News(), Source())
    return status, requests, post_repo


async def test_request_recorded_for_successful_attempt() -> None:
    status, requests, posts = await _run_with_requests([_answer()])
    assert status is NewsStatus.APPROVED
    [request] = requests.requests
    assert request.news_item_id == 1
    assert request.kind == "summary"
    assert request.model == CFG.model
    assert (request.prompt_tokens, request.cached_tokens, request.completion_tokens) == (
        1000,
        0,
        200,
    )
    assert request.ok is True
    assert request.cost == cost(1000, 0, 200, CFG)
    assert posts.posts[0].cost == request.cost


async def test_cached_tokens_reduce_request_cost() -> None:
    cfg = CFG.model_copy(update={"price_input_cached_per_1m": 0.0})
    news_repo, post_repo, requests = FakeNewsRepo(), FakePostRepo(), FakeRequestRepo()
    answer = AiRawResponse(_answer().content, 1000, 200, cached_tokens=800)
    processor = AiProcessor(
        FakeClient([answer]),
        PROFILE,
        _repos(news_repo, post_repo, requests),
        cfg.model_copy(update={"two_stage": False}),
    )
    assert await processor.process(News(), Source()) is NewsStatus.APPROVED
    [request] = requests.requests
    assert request.cached_tokens == 800
    assert request.cost == cost(1000, 800, 200, cfg)
    assert post_repo.posts[0].cost == request.cost


async def test_request_recorded_for_each_attempt() -> None:
    """Плохой первый ответ тоже оплачен: его токены видны в ai_requests."""
    status, requests, posts = await _run_with_requests([AiRawResponse("{}", 300, 20), _answer()])
    assert status is NewsStatus.APPROVED
    first, second = requests.requests
    assert (first.ok, first.prompt_tokens, first.completion_tokens) == (False, 300, 20)
    assert first.cost == cost(300, 0, 20, CFG)
    assert (second.ok, second.prompt_tokens) == (True, 1000)
    assert posts.posts[0].cost == first.cost + second.cost, "cost поста = сумма его запросов"


async def test_two_failed_attempts_are_both_recorded() -> None:
    status, requests, posts = await _run_with_requests(
        [AiRawResponse("{}", 300, 20), AiRawResponse("не json", 310, 25)]
    )
    assert status is NewsStatus.FAILED
    assert posts.posts == []
    assert [r.ok for r in requests.requests] == [False, False]
    assert sum(r.cost for r in requests.requests) == cost(300, 0, 20, CFG) + cost(310, 0, 25, CFG)
    assert all(r.news_item_id == 1 for r in requests.requests)


async def test_attempt_without_response_recorded_with_zeros() -> None:
    """Пустой или обрезанный ответ: токенов нет, но попытка видна."""
    status, requests, _ = await _run_with_requests(
        [AiResponseError("ответ модели обрезан по max_tokens"), _answer()]
    )
    assert status is NewsStatus.APPROVED
    first, second = requests.requests
    assert (first.ok, first.prompt_tokens, first.cached_tokens, first.completion_tokens) == (
        False,
        0,
        0,
        0,
    )
    assert first.cost == Decimal(0)
    assert second.ok is True


@pytest.mark.parametrize(
    "error", [AiUnavailableError("down"), AiFatalError("401")], ids=["unavailable", "fatal"]
)
async def test_api_errors_propagate_and_status_unchanged(error: Exception) -> None:
    news_repo, post_repo = FakeNewsRepo(), FakePostRepo()
    processor = AiProcessor(FakeClient([error]), PROFILE, _repos(news_repo, post_repo), LEGACY_CFG)
    with pytest.raises(type(error)):
        await processor.process(News(), Source())
    assert news_repo.status is NewsStatus.NORMALIZED
    assert post_repo.posts == []


def test_real_components_fit_protocols() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.ai_requests import AiRequestRepository
    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository
    from app.pipeline.ai.client import DeepSeekClient

    client = DeepSeekClient.__new__(DeepSeekClient)
    repos = AiRepos(
        news=NewsRepository(MagicMock()),
        posts=ProcessedPostRepository(MagicMock()),
        requests=AiRequestRepository(MagicMock()),
    )
    assert isinstance(AiProcessor(client, PROFILE, repos, CFG), AiProcessor)


class Admit:
    """Подменяет постановку в очередь: запоминает вызовы и отдаёт исход."""

    def __init__(self, outcome: AdmitOutcome = AdmitOutcome.ADMITTED) -> None:
        self.outcome = outcome
        self.calls: list[tuple[int, int]] = []

    async def __call__(self, news_id: int, source_id: int) -> AdmitResult:
        self.calls.append((news_id, source_id))
        status = (
            NewsStatus.FILTERED if self.outcome is AdmitOutcome.REJECTED else NewsStatus.APPROVED
        )
        return AdmitResult(self.outcome, status)


async def _run_with_admit(
    admit: Admit, mode: ModerationMode = ModerationMode.AUTO
) -> tuple[NewsStatus, FakeNewsRepo]:
    news_repo, post_repo = FakeNewsRepo(), FakePostRepo()
    processor = AiProcessor(
        FakeClient([_answer()]), PROFILE, _repos(news_repo, post_repo, admit=admit), LEGACY_CFG
    )
    status = await processor.process(News(), Source(mode))
    return status, news_repo


async def test_admit_called_with_news_and_source() -> None:
    admit = Admit()
    status, news = await _run_with_admit(admit)

    assert status is NewsStatus.APPROVED
    assert admit.calls == [(1, 7)]
    assert news.history == [NewsStatus.PROCESSED], "в approved переводит сам admit"


@pytest.mark.parametrize("outcome", [AdmitOutcome.ADMITTED, AdmitOutcome.ADMITTED_DISPLACED])
async def test_admitted_outcomes_give_approved(outcome: AdmitOutcome) -> None:
    status, _ = await _run_with_admit(Admit(outcome))
    assert status is NewsStatus.APPROVED


async def test_rejected_by_queue_limit_gives_filtered() -> None:
    with structlog.testing.capture_logs() as logs:
        status, _ = await _run_with_admit(Admit(AdmitOutcome.REJECTED))

    assert status is NewsStatus.FILTERED
    assert any(entry["event"] == "ai.queue_limit" for entry in logs)


async def test_review_mode_does_not_call_admit() -> None:
    """У источника с модерацией лимит проверяется при одобрении, а не здесь."""
    admit = Admit()
    status, _ = await _run_with_admit(admit, ModerationMode.REVIEW)
    assert status is NewsStatus.PENDING_REVIEW
    assert admit.calls == []


async def test_without_admit_behaviour_unchanged() -> None:
    status, news, _, _ = await _run([_answer()])
    assert status is NewsStatus.APPROVED
    assert news.history == [NewsStatus.PROCESSED, NewsStatus.APPROVED]
