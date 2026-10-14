import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from app.config import AiConfig, ChannelProfile
from app.db.models import ProcessedPost
from app.domain.enums import ModerationMode, NewsStatus
from app.domain.status import ensure_transition
from app.pipeline.ai.budget import cost
from app.pipeline.ai.client import (
    AiFatalError,
    AiRawResponse,
    AiResponseError,
    AiUnavailableError,
    Message,
)
from app.pipeline.ai.processor import AiProcessor, AiRepos
from app.pipeline.ai.prompts import PROMPT_VERSION
from tests.config_helpers import base_section

CFG = AiConfig(**{**base_section("ai"), "min_score": 0.5})
PROFILE = ChannelProfile(**{**base_section("channel"), "hashtags_max": 2})
SOURCE_TEXT = (
    "Компания Nvidia представила новый процессор. По данным компании, он на 30% быстрее "
    "предыдущей модели и поступит в продажу в 2027 году."
)


@dataclass
class News:
    id: int = 1
    title: str = "Nvidia представила процессор"
    url: str = "https://news.example/1"
    clean_text: str | None = SOURCE_TEXT
    raw_text: str = SOURCE_TEXT
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


class FakeClient:
    def __init__(self, outcomes: list[AiRawResponse | Exception]) -> None:
        self.outcomes: list[AiRawResponse | Exception] = list(outcomes)
        self.calls: list[list[Message]] = []

    async def chat_json(self, messages: list[Message]) -> AiRawResponse:
        self.calls.append(messages)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _answer(**overrides: Any) -> AiRawResponse:
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


async def _run(
    outcomes: list[Any], mode: ModerationMode = ModerationMode.AUTO
) -> tuple[NewsStatus, FakeNewsRepo, FakePostRepo, FakeClient]:
    news_repo, post_repo, client = FakeNewsRepo(), FakePostRepo(), FakeClient(outcomes)
    processor = AiProcessor(client, PROFILE, AiRepos(news=news_repo, posts=post_repo), CFG)
    status = await processor.process(News(), Source(mode))
    return status, news_repo, post_repo, client


async def test_auto_mode_approved() -> None:
    status, news, posts, _ = await _run([_answer()])
    assert status is NewsStatus.APPROVED
    assert news.history == [NewsStatus.PROCESSED, NewsStatus.APPROVED]
    [post] = posts.posts
    assert post.news_item_id == 1
    assert post.ai_title == "Nvidia показала новый процессор"
    assert post.hashtags == ["#железо"]
    assert post.prompt_version == PROMPT_VERSION
    assert (post.prompt_tokens, post.completion_tokens) == (1000, 200)
    assert post.cost == cost(1000, 200, CFG), "стоимость считается по ценам из конфигурации"
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


async def test_second_bad_response_fails() -> None:
    status, news, posts, client = await _run(
        [AiRawResponse("{}", 10, 5), AiResponseError("пустой")]
    )
    assert status is NewsStatus.FAILED
    assert news.reason == "bad_ai_response"
    assert news.history == [NewsStatus.FAILED]
    assert posts.posts == []
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "error", [AiUnavailableError("down"), AiFatalError("401")], ids=["unavailable", "fatal"]
)
async def test_api_errors_propagate_and_status_unchanged(error: Exception) -> None:
    news_repo, post_repo = FakeNewsRepo(), FakePostRepo()
    processor = AiProcessor(
        FakeClient([error]), PROFILE, AiRepos(news=news_repo, posts=post_repo), CFG
    )
    with pytest.raises(type(error)):
        await processor.process(News(), Source())
    assert news_repo.status is NewsStatus.NORMALIZED
    assert post_repo.posts == []


def test_real_components_fit_protocols() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository
    from app.pipeline.ai.client import DeepSeekClient

    client = DeepSeekClient.__new__(DeepSeekClient)
    repos = AiRepos(news=NewsRepository(MagicMock()), posts=ProcessedPostRepository(MagicMock()))
    assert isinstance(AiProcessor(client, PROFILE, repos, CFG), AiProcessor)
