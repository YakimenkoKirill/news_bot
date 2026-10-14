from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import structlog

from app.config import PublisherConfig
from app.db.models import ProcessedPost, Publication
from app.domain.alerts import AlertEvent, AlertKind
from app.domain.enums import NewsStatus, PublicationStatus
from app.domain.status import ensure_transition
from app.publisher.limits import FittedPost
from app.publisher.media import ImageCheck
from app.publisher.publisher import PublishError
from app.publisher.stage_publish import (
    ManualOutcome,
    ManualPublishDeps,
    ManualPublishResult,
    PublishDeps,
    publish_due,
    publish_now,
)
from tests.config_helpers import base_section

CFG = PublisherConfig(**{**base_section("publisher"), "quiet_hours": ["23:00", "07:00"]})
CHANNEL = -100777
TZ = "Europe/Moscow"
NOON = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)  # 12:00 по Москве
NIGHT = datetime(2026, 10, 10, 21, 0, tzinfo=UTC)  # 00:00 по Москве
SECONDS_PER_HOUR = 3600
ADMIN = 42
S = NewsStatus


@dataclass
class News:
    id: int
    source_id: int = 1
    status: NewsStatus = S.APPROVED
    url: str = "https://src.example/a"
    image_url: str | None = "https://cdn.example/a.jpg"
    created_at: datetime = NOON - timedelta(hours=1)
    published_at: datetime | None = None


@dataclass
class Source:
    name: str = "Пример"


@dataclass
class Db:
    news: dict[int, News] = field(default_factory=dict)
    posts: dict[int, ProcessedPost] = field(default_factory=dict)
    publications: list[Publication] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    sources: dict[int, Source] = field(default_factory=lambda: {1: Source()})
    reasons: dict[int, str] = field(default_factory=dict)
    busy: set[int] = field(default_factory=set)
    transitions: list[tuple[int, NewsStatus, NewsStatus]] = field(default_factory=list)


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def claim_next(self, statuses: Sequence[NewsStatus], decay_per_h: float) -> News | None:
        queued = [
            n for n in self.db.news.values() if n.status in statuses and n.id not in self.db.busy
        ]

        def priority(n: News) -> float:
            moment = n.published_at or n.created_at
            post = self.db.posts.get(n.id)
            score = post.relevance_score if post is not None else 0.0
            return score + decay_per_h * moment.timestamp() / SECONDS_PER_HOUR

        ordered = sorted(queued, key=lambda n: (-priority(n), n.id))
        return ordered[0] if ordered else None

    async def claim_one(self, news_id: int, statuses: Sequence[NewsStatus]) -> News | None:
        item = self.db.news.get(news_id)
        if item is None or item.status not in statuses or news_id in self.db.busy:
            return None
        return item

    async def get(self, news_id: int) -> News | None:
        return self.db.news.get(news_id)

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        item = self.db.news[news_id]
        assert item.status == expected
        ensure_transition(expected, new)
        item.status = new
        self.db.transitions.append((news_id, expected, new))
        if reason:
            self.db.reasons[news_id] = reason


class PostRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        return self.db.posts.get(news_id)


class SourceRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, source_id: int) -> Source | None:
        return self.db.sources.get(source_id)


class PublicationRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def add(self, publication: Publication) -> Publication:
        self.db.publications.append(publication)
        return publication

    async def last_published_at(self, channel_id: int) -> datetime | None:
        times = [
            p.published_at
            for p in self.db.publications
            if p.status is PublicationStatus.SENT and p.published_at and p.channel_id == channel_id
        ]
        return max(times) if times else None


class SettingsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, key: str, default: Any = None) -> Any:
        return self.db.settings.get(key, default)


class Sender:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[FittedPost, str | None, str | None]] = []

    async def send(self, fitted: FittedPost, image_url: str | None, link: str | None = None) -> int:
        self.calls.append((fitted, image_url, link))
        if self.error:
            raise self.error
        return 9001


class Sink:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    async def emit(self, event: AlertEvent) -> None:
        self.events.append(event)


def _post(news_id: int, score: float = 0.9) -> ProcessedPost:
    return ProcessedPost(
        id=news_id * 10,
        news_item_id=news_id,
        ai_title="Заголовок",
        ai_summary="Пересказ новости.",
        hashtags=["#тег"],
        relevance_score=score,
        prompt_version="v1",
        prompt_tokens=1,
        completion_tokens=1,
        cost=Decimal("0.001"),
    )


@dataclass
class Env:
    db: Db
    sender: Sender
    sink: Sink
    deps: PublishDeps
    manual: ManualPublishDeps


def _env(
    news: list[News],
    sender: Sender | None = None,
    scores: dict[int, float] | None = None,
) -> Env:
    scores = scores or {}
    posts = {n.id: _post(n.id, scores.get(n.id, 0.9)) for n in news}
    db = Db(news={n.id: n for n in news}, posts=posts)
    sender = sender or Sender()
    sink = Sink()

    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    async def check(url: str | None) -> ImageCheck:
        return ImageCheck(ok=True)

    deps = PublishDeps(
        session_scope=scope,
        news_repo=NewsRepo,
        post_repo=PostRepo,
        source_repo=SourceRepo,
        publication_repo=PublicationRepo,
        settings_repo=SettingsRepo,
        check_image=check,
        publisher=sender,
        sink=sink,
        cfg=CFG,
        channel_id=CHANNEL,
        tz=TZ,
    )
    return Env(db, sender, sink, deps, ManualPublishDeps(publish=deps, news_repo=NewsRepo))


async def test_publishes_approved_immediately() -> None:
    env = _env([News(1)])
    result = await publish_now(1, NOON, env.manual, ADMIN)
    assert result == ManualPublishResult(ManualOutcome.PUBLISHED, 1, message_id=9001)
    assert env.db.news[1].status is S.PUBLISHED
    [pub] = env.db.publications
    assert (pub.status, pub.message_id, pub.published_at) == (PublicationStatus.SENT, 9001, NOON)
    fitted, _, link = env.sender.calls[0]
    assert link is None
    assert "<b>Заголовок</b>" in fitted.html


async def test_scheduled_published() -> None:
    env = _env([News(1, status=S.SCHEDULED)])
    assert (await publish_now(1, NOON, env.manual, ADMIN)).outcome is ManualOutcome.PUBLISHED
    assert env.db.news[1].status is S.PUBLISHED


async def test_pending_review_goes_through_approved() -> None:
    env = _env([News(1, status=S.PENDING_REVIEW)])
    result = await publish_now(1, NOON, env.manual, ADMIN)
    assert result.outcome is ManualOutcome.PUBLISHED
    assert env.db.transitions == [
        (1, S.PENDING_REVIEW, S.APPROVED),
        (1, S.APPROVED, S.PUBLISHED),
    ]


async def test_already_published_is_not_sent_again() -> None:
    env = _env([News(1)])
    await publish_now(1, NOON, env.manual, ADMIN)
    again = await publish_now(1, NOON, env.manual, ADMIN)
    assert again == ManualPublishResult(ManualOutcome.ALREADY_DONE, 1, status=S.PUBLISHED)
    assert len(env.sender.calls) == 1
    assert len(env.db.publications) == 1


@pytest.mark.parametrize("status", [S.REJECTED, S.FILTERED, S.FAILED, S.NORMALIZED])
async def test_other_statuses_already_done(status: NewsStatus) -> None:
    env = _env([News(1, status=status)])
    result = await publish_now(1, NOON, env.manual, ADMIN)
    assert result == ManualPublishResult(ManualOutcome.ALREADY_DONE, 1, status=status)
    assert env.sender.calls == []


async def test_busy_row_returns_busy() -> None:
    env = _env([News(1)])
    env.db.busy.add(1)
    result = await publish_now(1, NOON, env.manual, ADMIN)
    assert result.outcome is ManualOutcome.BUSY
    assert env.sender.calls == []
    assert env.db.news[1].status is S.APPROVED


async def test_not_found() -> None:
    env = _env([])
    assert await publish_now(7, NOON, env.manual, ADMIN) == ManualPublishResult(
        ManualOutcome.NOT_FOUND, 7
    )


async def test_publish_error_marks_failed() -> None:
    env = _env([News(1)], sender=Sender(PublishError("Telegram отклонил пост: chat not found")))
    result = await publish_now(1, NOON, env.manual, ADMIN)
    assert result.outcome is ManualOutcome.FAILED
    assert result.error is not None and "chat not found" in result.error
    assert env.db.news[1].status is S.FAILED
    assert env.db.reasons[1] == "publish_error"
    [pub] = env.db.publications
    assert pub.status is PublicationStatus.FAILED
    [event] = env.sink.events
    assert event.kind is AlertKind.PUBLISH_ERROR


async def test_ignores_quiet_hours() -> None:
    env = _env([News(1)])
    assert (await publish_now(1, NIGHT, env.manual, ADMIN)).outcome is ManualOutcome.PUBLISHED


async def test_ignores_pause() -> None:
    env = _env([News(1)])
    env.db.settings["paused"] = True
    assert (await publish_now(1, NOON, env.manual, ADMIN)).outcome is ManualOutcome.PUBLISHED


async def test_ignores_interval() -> None:
    env = _env([News(1), News(2)])
    await publish_due(NOON, env.deps)
    result = await publish_now(2, NOON + timedelta(minutes=1), env.manual, ADMIN)
    assert result.outcome is ManualOutcome.PUBLISHED
    assert len(env.sender.calls) == 2


async def test_publish_due_slot_moves_after_manual() -> None:
    env = _env([News(1), News(2)])
    await publish_now(1, NOON, env.manual, ADMIN)
    report = await publish_due(NOON, env.deps)
    assert report.skipped == "slot"
    assert report.next_slot == NOON + timedelta(minutes=CFG.min_interval_min)


async def test_manual_lowest_priority_keeps_queue_order() -> None:
    env = _env([News(1), News(2), News(3)], scores={1: 0.9, 2: 0.7, 3: 0.5})
    assert (await publish_now(3, NOON, env.manual, ADMIN)).outcome is ManualOutcome.PUBLISHED
    after_interval = NOON + timedelta(minutes=CFG.min_interval_min)
    assert (await publish_due(after_interval, env.deps)).news_id == 1
    later = after_interval + timedelta(minutes=CFG.min_interval_min)
    assert (await publish_due(later, env.deps)).news_id == 2


async def test_manual_publish_is_logged() -> None:
    env = _env([News(1)])
    with structlog.testing.capture_logs() as logs:
        await publish_now(1, NOON, env.manual, ADMIN)
    [entry] = [e for e in logs if e["event"] == "publish.manual"]
    assert (entry["news_id"], entry["initiator_id"], entry["outcome"]) == (1, ADMIN, "published")


def test_real_repository_fits_manual_protocol() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.news import NewsRepository

    env = _env([])
    deps = ManualPublishDeps(publish=env.deps, news_repo=NewsRepository)
    assert deps.news_repo is NewsRepository
    assert isinstance(NewsRepository(MagicMock()), NewsRepository)
