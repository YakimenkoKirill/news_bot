import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.db.models import AiRequest, ProcessedPost
from app.domain.alerts import AlertEvent, AlertKind
from app.domain.enums import ModerationMode, NewsStatus
from app.pipeline.ai.budget import BudgetExceeded
from app.pipeline.ai.client import AiFatalError, AiUnavailableError
from app.pipeline.ai.processor import AiRepos, ModeratedSource, ProcessableNews
from app.pipeline.queue_limit import AdmitOutcome, AdmitResult
from app.pipeline.stage_ai import AiStageDeps, AiStageReport, process_normalized_batch

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@dataclass
class News:
    id: int
    source_id: int = 1
    title: str = "t"
    url: str = "https://e.com/1"
    clean_text: str | None = "text"
    raw_text: str = "text"
    published_at: datetime | None = None
    status: NewsStatus = NewsStatus.NORMALIZED


@dataclass
class Src:
    moderation_mode: ModerationMode = ModerationMode.AUTO


@dataclass
class Db:
    news: dict[int, News] = field(default_factory=dict)
    posts: dict[int, ProcessedPost] = field(default_factory=dict)
    requests: list[AiRequest] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    sources: dict[int, Src] = field(default_factory=lambda: {1: Src()})


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def claim_batch(self, status: NewsStatus, limit: int) -> Sequence[News]:
        return [n for n in sorted(self.db.news.values(), key=lambda n: n.id) if n.status == status][
            :limit
        ]

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        assert self.db.news[news_id].status == expected
        self.db.news[news_id].status = new


class SourceRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, source_id: int) -> Src | None:
        return self.db.sources.get(source_id)


class PostRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def add(self, post: ProcessedPost) -> ProcessedPost:
        self.db.posts[post.news_item_id] = post
        return post

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        return self.db.posts.get(news_id)


class RequestRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def add(self, request: AiRequest) -> AiRequest:
        self.db.requests.append(request)
        return request


class SettingsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, key: str, default: Any = None) -> Any:
        return self.db.settings.get(key, default)


class FakeProcessor:
    """Обработчик: исход по id новости (статус или исключение)."""

    def __init__(
        self, repos: AiRepos, plan: dict[int, NewsStatus | Exception], tracker: "Tracker"
    ) -> None:
        self.repos = repos
        self.plan = plan
        self.tracker = tracker

    async def process(self, news: ProcessableNews, source: ModeratedSource) -> NewsStatus:
        self.tracker.active += 1
        self.tracker.max_active = max(self.tracker.max_active, self.tracker.active)
        try:
            await asyncio.sleep(0)
            outcome = self.plan.get(news.id, NewsStatus.APPROVED)
            self.tracker.processed.append(news.id)
            if isinstance(outcome, Exception):
                raise outcome
            await self.repos.posts.add(
                ProcessedPost(
                    news_item_id=news.id,
                    ai_title="t",
                    ai_summary="s",
                    hashtags=[],
                    relevance_score=0.9,
                    prompt_version="v1",
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost=Decimal("0.01"),
                )
            )
            path = {
                NewsStatus.APPROVED: [NewsStatus.PROCESSED, NewsStatus.APPROVED],
                NewsStatus.PENDING_REVIEW: [NewsStatus.PROCESSED, NewsStatus.PENDING_REVIEW],
                NewsStatus.FILTERED: [NewsStatus.FILTERED],
                NewsStatus.FAILED: [NewsStatus.FAILED],
            }[outcome]
            current = NewsStatus.NORMALIZED
            for status in path:
                await self.repos.news.set_status(news.id, current, status)
                current = status
            return outcome
        finally:
            self.tracker.active -= 1


@dataclass
class Tracker:
    processed: list[int] = field(default_factory=list)
    active: int = 0
    max_active: int = 0


class Budget:
    def __init__(self, exceed_after: int | None = None) -> None:
        self.exceed_after = exceed_after
        self.calls = 0

    async def check(self, now: datetime) -> Decimal:
        self.calls += 1
        if self.exceed_after is not None and self.calls > self.exceed_after:
            raise BudgetExceeded(Decimal("1"), Decimal("1"))
        return Decimal("0")


class Notifier:
    def __init__(self, fail: bool = False) -> None:
        self.posts: list[ProcessedPost] = []
        self.fail = fail

    async def notify(self, post: ProcessedPost) -> None:
        self.posts.append(post)
        if self.fail:
            raise RuntimeError("telegram down")


class Sink:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[AlertEvent] = []
        self.fail = fail

    async def emit(self, event: AlertEvent) -> None:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("telegram down")


@dataclass
class Env:
    db: Db
    deps: AiStageDeps
    tracker: Tracker
    budget: Budget
    notifier: Notifier
    sink: Sink


def _env(
    count: int,
    plan: dict[int, NewsStatus | Exception] | None = None,
    budget: Budget | None = None,
    notifier: Notifier | None = None,
    concurrency: int = 2,
    sink: Sink | None = None,
) -> Env:
    db = Db(news={i: News(i) for i in range(1, count + 1)})
    tracker = Tracker()
    budget = budget or Budget()
    notifier = notifier or Notifier()
    sink = sink or Sink()

    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    deps = AiStageDeps(
        session_scope=scope,
        news_repo=NewsRepo,
        source_repo=SourceRepo,
        post_repo=PostRepo,
        request_repo=RequestRepo,
        settings_repo=SettingsRepo,
        processor=lambda repos: FakeProcessor(repos, plan or {}, tracker),
        budget=budget,
        notifier=notifier,
        sink=sink,
        batch_size=20,
        max_concurrency=concurrency,
    )
    return Env(db, deps, tracker, budget, notifier, sink)


async def test_batch_processed_with_report() -> None:
    env = _env(4, plan={2: NewsStatus.PENDING_REVIEW, 3: NewsStatus.FILTERED})
    report = await process_normalized_batch(NOW, env.deps)
    assert report == AiStageReport(
        claimed=4,
        statuses={
            NewsStatus.APPROVED: 2,
            NewsStatus.PENDING_REVIEW: 1,
            NewsStatus.FILTERED: 1,
        },
    )
    assert env.db.news[1].status is NewsStatus.APPROVED


async def test_paused_does_nothing() -> None:
    env = _env(3)
    env.db.settings["paused"] = True
    assert await process_normalized_batch(NOW, env.deps) == AiStageReport(stopped="paused")
    assert env.tracker.processed == []
    assert env.budget.calls == 0


async def test_budget_exceeded_before_batch() -> None:
    env = _env(3, budget=Budget(exceed_after=0))
    report = await process_normalized_batch(NOW, env.deps)
    assert report == AiStageReport(stopped="budget")
    assert all(n.status is NewsStatus.NORMALIZED for n in env.db.news.values())


async def test_budget_exceeded_during_batch_leaves_rest_normalized() -> None:
    env = _env(5, budget=Budget(exceed_after=3), concurrency=1)
    report = await process_normalized_batch(NOW, env.deps)
    assert report.stopped == "budget"
    assert report.statuses == {NewsStatus.APPROVED: 2}
    assert report.skipped == 3
    assert [n.status for n in env.db.news.values()].count(NewsStatus.NORMALIZED) == 3


@pytest.mark.parametrize("error", [AiUnavailableError("down"), AiFatalError("402")])
async def test_ai_down_alerts_once_and_stops(error: Exception) -> None:
    env = _env(6, plan={1: error, 2: error, 3: error}, concurrency=3)
    report = await process_normalized_batch(NOW, env.deps)
    assert report.stopped == "ai_down"
    [event] = env.sink.events
    assert event.kind is AlertKind.AI_DOWN
    assert env.db.news[1].status is NewsStatus.NORMALIZED
    assert report.skipped >= 3
    assert sum(report.statuses.values()) + report.skipped + report.errors == 6


async def test_review_notifier_called_after_pending_review() -> None:
    env = _env(3, plan={1: NewsStatus.PENDING_REVIEW, 3: NewsStatus.PENDING_REVIEW})
    await process_normalized_batch(NOW, env.deps)
    assert sorted(p.news_item_id for p in env.notifier.posts) == [1, 3]


async def test_notifier_error_does_not_break_batch() -> None:
    env = _env(2, plan={1: NewsStatus.PENDING_REVIEW}, notifier=Notifier(fail=True))
    report = await process_normalized_batch(NOW, env.deps)
    assert report.statuses == {NewsStatus.PENDING_REVIEW: 1, NewsStatus.APPROVED: 1}
    assert report.errors == 0


async def test_one_item_error_does_not_break_others() -> None:
    env = _env(3, plan={2: RuntimeError("db hiccup")})
    report = await process_normalized_batch(NOW, env.deps)
    assert report.errors == 1
    assert report.statuses == {NewsStatus.APPROVED: 2}
    assert report.stopped is None
    assert env.db.news[2].status is NewsStatus.NORMALIZED


def _item_failed(env: Env) -> list[AlertEvent]:
    return [e for e in env.sink.events if e.kind is AlertKind.AI_ITEM_FAILED]


async def test_failed_status_alerts_item_failed() -> None:
    env = _env(2, plan={1: NewsStatus.FAILED})
    report = await process_normalized_batch(NOW, env.deps)
    assert report.statuses == {NewsStatus.FAILED: 1, NewsStatus.APPROVED: 1}

    [event] = _item_failed(env)
    assert event.key == "ai_item_failed:1"
    assert "bad_ai_response" in event.message
    assert "id 1" in event.message
    assert event.created_at == NOW


async def test_item_exception_alerts_item_failed() -> None:
    env = _env(2, plan={2: RuntimeError("db hiccup")})
    report = await process_normalized_batch(NOW, env.deps)
    assert report.errors == 1

    [event] = _item_failed(env)
    assert event.key == "ai_item_failed:2"
    assert "db hiccup" in event.message


async def test_alert_error_does_not_break_batch() -> None:
    env = _env(3, plan={2: NewsStatus.FAILED}, sink=Sink(fail=True))
    report = await process_normalized_batch(NOW, env.deps)
    assert report.statuses == {NewsStatus.FAILED: 1, NewsStatus.APPROVED: 2}
    assert report.errors == 0
    assert len(_item_failed(env)) == 1


async def test_successful_items_do_not_alert() -> None:
    env = _env(3, plan={2: NewsStatus.FILTERED, 3: NewsStatus.PENDING_REVIEW})
    await process_normalized_batch(NOW, env.deps)
    assert _item_failed(env) == []


async def test_concurrency_limit() -> None:
    env = _env(8, concurrency=3)
    await process_normalized_batch(NOW, env.deps)
    assert env.tracker.max_active == 3
    assert len(env.tracker.processed) == 8


async def test_empty_queue() -> None:
    env = _env(0)
    assert await process_normalized_batch(NOW, env.deps) == AiStageReport()


async def test_missing_source_skips_item() -> None:
    env = _env(1)
    env.db.news[1].source_id = 99
    report = await process_normalized_batch(NOW, env.deps)
    assert report.statuses == {}
    assert env.db.news[1].status is NewsStatus.NORMALIZED


def test_real_components_fit_protocols() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import AiConfig, ChannelProfile
    from app.db.repositories.ai_requests import AiRequestRepository
    from app.db.repositories.misc import SettingsRepository
    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository
    from app.db.repositories.sources import SourceRepository
    from app.pipeline.ai.budget import BudgetGuard
    from app.pipeline.ai.client import DeepSeekClient
    from app.pipeline.ai.processor import AiProcessor
    from tests.config_helpers import base_section

    cfg = AiConfig(**base_section("ai"))
    profile = ChannelProfile(**base_section("channel"))
    client = DeepSeekClient.__new__(DeepSeekClient)

    def processor(repos: AiRepos) -> AiProcessor:
        return AiProcessor(client, profile, repos, cfg)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        raise AssertionError("не вызывается")
        yield

    cost_repo = AiRequestRepository.__new__(AiRequestRepository)
    budget = BudgetGuard(cost_repo, Sink(), cfg, "UTC")
    deps = AiStageDeps(
        session_scope=scope,
        news_repo=NewsRepository,
        source_repo=SourceRepository,
        post_repo=ProcessedPostRepository,
        request_repo=AiRequestRepository,
        settings_repo=SettingsRepository,
        processor=processor,
        budget=budget,
        notifier=Notifier(),
        sink=Sink(),
        batch_size=1,
        max_concurrency=1,
    )
    assert deps.batch_size == 1


async def test_admission_passed_to_processor() -> None:
    """Стадия отдаёт процессору постановку в очередь с expected=processed."""
    seen: list[tuple[int, int, NewsStatus, bool]] = []

    class Admission:
        async def admit(
            self,
            session: Any,
            news_id: int,
            source_id: int,
            expected: NewsStatus,
            *,
            forced: bool = False,
        ) -> AdmitResult:
            seen.append((news_id, source_id, expected, forced))
            return AdmitResult(AdmitOutcome.ADMITTED, NewsStatus.APPROVED)

    env = _env(2)
    deps = replace(env.deps, admission=Admission())  # type: ignore[arg-type]

    captured: list[AiRepos] = []
    original = deps.processor

    def processor(repos: AiRepos) -> Any:
        captured.append(repos)
        return original(repos)

    await process_normalized_batch(NOW, replace(deps, processor=processor))

    assert captured and all(repos.admit is not None for repos in captured)
    for repos in captured:
        assert repos.admit is not None
        await repos.admit(1, 5)
    assert all(call[2] is NewsStatus.PROCESSED and call[3] is False for call in seen)


async def test_without_admission_processor_gets_none() -> None:
    captured: list[AiRepos] = []
    env = _env(1)
    original = env.deps.processor

    def processor(repos: AiRepos) -> Any:
        captured.append(repos)
        return original(repos)

    await process_normalized_batch(NOW, replace(env.deps, processor=processor))
    assert captured and all(repos.admit is None for repos in captured)
