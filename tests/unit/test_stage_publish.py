from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.config import PublisherConfig
from app.db.models import ProcessedPost, Publication
from app.domain.alerts import AlertEvent, AlertKind
from app.domain.enums import NewsStatus, PublicationStatus
from app.domain.status import ensure_transition
from app.publisher.limits import FittedPost
from app.publisher.media import ImageCheck
from app.publisher.publisher import PublishError
from app.publisher.stage_publish import PublishDeps, PublishReport, publish_due
from tests.config_helpers import base_section

CFG = PublisherConfig(**{**base_section("publisher"), "quiet_hours": ["23:00", "07:00"]})
CHANNEL = -100777
TZ = "Europe/Moscow"
NOON = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)  # 12:00 по Москве


@dataclass
class News:
    id: int
    source_id: int = 1
    status: NewsStatus = NewsStatus.APPROVED
    url: str = "https://src.example/a"
    image_url: str | None = "https://cdn.example/a.jpg"
    created_at: datetime = NOON - timedelta(hours=1)


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


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def claim_batch(
        self, status: NewsStatus | Sequence[NewsStatus], limit: int
    ) -> Sequence[News]:
        statuses = [status] if isinstance(status, NewsStatus) else list(status)
        queued = [n for n in self.db.news.values() if n.status in statuses]
        return sorted(queued, key=lambda n: (n.created_at, n.id))[:limit]

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        item = self.db.news[news_id]
        assert item.status == expected
        ensure_transition(expected, new)
        item.status = new
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


def _post(news_id: int) -> ProcessedPost:
    return ProcessedPost(
        id=news_id * 10,
        news_item_id=news_id,
        ai_title="Заголовок",
        ai_summary="Пересказ новости.",
        hashtags=["#тег"],
        relevance_score=0.9,
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
    image_checks: list[str | None]


def _env(news: list[News], image_ok: bool = True, sender: Sender | None = None) -> Env:
    db = Db(news={n.id: n for n in news}, posts={n.id: _post(n.id) for n in news})
    sender = sender or Sender()
    sink = Sink()
    checks: list[str | None] = []

    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    async def check(url: str | None) -> ImageCheck:
        checks.append(url)
        return ImageCheck(ok=True) if image_ok else ImageCheck(ok=False, reason="bad_type:svg")

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
    return Env(db, sender, sink, deps, checks)


async def test_successful_publication_one_per_call() -> None:
    env = _env([News(1), News(2, created_at=NOON - timedelta(hours=2))])
    report = await publish_due(NOON, env.deps)
    assert report == PublishReport(news_id=2, message_id=9001)
    assert env.db.news[2].status is NewsStatus.PUBLISHED
    assert env.db.news[1].status is NewsStatus.APPROVED
    [pub] = env.db.publications
    assert pub.status is PublicationStatus.SENT
    assert (pub.message_id, pub.published_at, pub.processed_post_id) == (9001, NOON, 20)
    fitted, image_url, link = env.sender.calls[0]
    assert fitted.use_photo is True
    assert image_url == "https://cdn.example/a.jpg"
    assert link == "https://src.example/a"
    assert "<b>Заголовок</b>" in fitted.html


async def test_scheduled_post_published() -> None:
    env = _env([News(1, status=NewsStatus.SCHEDULED)])
    assert (await publish_due(NOON, env.deps)).message_id == 9001
    assert env.db.news[1].status is NewsStatus.PUBLISHED


async def test_slot_in_future_by_interval() -> None:
    env = _env([News(1), News(2)])
    await publish_due(NOON, env.deps)
    report = await publish_due(NOON + timedelta(minutes=5), env.deps)
    assert report.skipped == "slot"
    assert report.next_slot == NOON + timedelta(minutes=CFG.min_interval_min)
    assert len(env.sender.calls) == 1
    later = await publish_due(NOON + timedelta(minutes=CFG.min_interval_min), env.deps)
    assert later.message_id == 9001


async def test_quiet_hours_block_publication() -> None:
    night = datetime(2026, 10, 10, 21, 0, tzinfo=UTC)  # 00:00 по Москве
    env = _env([News(1)])
    report = await publish_due(night, env.deps)
    assert report.skipped == "slot"
    assert report.next_slot == datetime(2026, 10, 11, 4, 0, tzinfo=UTC)
    assert env.sender.calls == []


async def test_paused() -> None:
    env = _env([News(1)])
    env.db.settings["paused"] = True
    assert await publish_due(NOON, env.deps) == PublishReport(skipped="paused")
    assert env.sender.calls == []


async def test_send_error_marks_failed_and_alerts() -> None:
    env = _env([News(1)], sender=Sender(PublishError("Telegram отклонил пост: chat not found")))
    report = await publish_due(NOON, env.deps)
    assert report.news_id == 1
    assert report.error is not None
    assert "chat not found" in report.error
    assert env.db.news[1].status is NewsStatus.FAILED
    assert env.db.reasons[1] == "publish_error"
    [pub] = env.db.publications
    assert pub.status is PublicationStatus.FAILED
    assert pub.error is not None
    [event] = env.sink.events
    assert event.kind is AlertKind.PUBLISH_ERROR


async def test_bad_image_published_without_photo() -> None:
    env = _env([News(1)], image_ok=False)
    await publish_due(NOON, env.deps)
    fitted, _, _ = env.sender.calls[0]
    assert fitted.use_photo is False
    assert env.image_checks == ["https://cdn.example/a.jpg"]


async def test_empty_queue() -> None:
    env = _env([])
    assert await publish_due(NOON, env.deps) == PublishReport(skipped="empty")


async def test_missing_post_marks_failed() -> None:
    env = _env([News(1)])
    env.db.posts.clear()
    report = await publish_due(NOON, env.deps)
    assert report.error == "no_processed_post"
    assert env.db.news[1].status is NewsStatus.FAILED
    assert env.sender.calls == []


def test_real_components_fit_protocols() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.misc import SettingsRepository
    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
    from app.db.repositories.sources import SourceRepository
    from app.publisher.publisher import Publisher

    async def check(url: str | None) -> ImageCheck:
        return ImageCheck(ok=False, reason="no_image")

    deps = PublishDeps(
        session_scope=MagicMock(),
        news_repo=NewsRepository,
        post_repo=ProcessedPostRepository,
        source_repo=SourceRepository,
        publication_repo=PublicationRepository,
        settings_repo=SettingsRepository,
        check_image=check,
        publisher=Publisher(MagicMock(), CHANNEL, CFG),
        sink=Sink(),
        cfg=CFG,
        channel_id=CHANNEL,
        tz=TZ,
    )
    assert deps.channel_id == CHANNEL
