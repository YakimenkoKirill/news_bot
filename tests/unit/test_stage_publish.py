from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog.testing

from app.config import AiConfig, ChannelProfile, PublisherConfig
from app.db.models import ProcessedPost, Publication
from app.db.repositories.news import Priority, StuckRef
from app.domain.alerts import AlertEvent, AlertKind
from app.domain.enums import NewsStatus, PublicationStatus
from app.domain.status import ensure_transition
from app.publisher.limits import FittedPost
from app.publisher.media import ImageCheck
from app.publisher.publisher import PublishError
from app.publisher.stage_publish import (
    PublishDeps,
    PublishReport,
    publish_due,
    recover_stuck,
)
from app.runtime_config import RuntimeSnapshot
from tests.config_helpers import base_section

CFG = PublisherConfig(**{**base_section("publisher"), "quiet_hours": ["23:00", "07:00"]})
CHANNEL = -100777
TZ = "Europe/Moscow"
NOON = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)  # 12:00 по Москве
SECONDS_PER_HOUR = 3600


@dataclass
class News:
    id: int
    source_id: int = 1
    status: NewsStatus = NewsStatus.APPROVED
    url: str = "https://src.example/a"
    image_url: str | None = "https://cdn.example/a.jpg"
    created_at: datetime = NOON - timedelta(hours=1)
    published_at: datetime | None = None
    updated_at: datetime = NOON
    cluster_sources: int = 1
    raw_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Source:
    name: str = "Пример"


@dataclass
class Db:
    news: dict[int, News] = field(default_factory=dict)
    open_scopes: int = 0
    posts: dict[int, ProcessedPost] = field(default_factory=dict)
    publications: list[Publication] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    sources: dict[int, Source] = field(default_factory=lambda: {1: Source()})
    reasons: dict[int, str] = field(default_factory=dict)


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def claim_next(self, statuses: Sequence[NewsStatus], priority: Priority) -> News | None:
        queued = [n for n in self.db.news.values() if n.status in statuses]

        def key(n: News) -> float:
            moment = n.published_at or n.created_at
            post = self.db.posts.get(n.id)
            score = post.relevance_score if post is not None else 0.0
            bonus = priority.cluster_bonus * min(
                max(n.cluster_sources - 1, 0), priority.cluster_bonus_max
            )
            return score + priority.decay_per_h * moment.timestamp() / SECONDS_PER_HOUR + bonus

        ordered = sorted(queued, key=lambda n: (-key(n), n.id))
        return ordered[0] if ordered else None

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        item = self.db.news[news_id]
        assert item.status == expected
        ensure_transition(expected, new)
        item.status = new
        if reason:
            self.db.reasons[news_id] = reason

    async def list_stuck_scheduled(self, before: datetime) -> list[StuckRef]:
        return [
            StuckRef(news_id=n.id, title=f"Новость {n.id}", sent=False)
            for n in self.db.news.values()
            if n.status is NewsStatus.SCHEDULED and n.updated_at <= before
        ]


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
        self.calls: list[tuple[FittedPost, str | bytes | None, str | None]] = []
        self.open_scopes: list[int] = []
        self.db: Db | None = None

    async def send(
        self, fitted: FittedPost, photo: str | bytes | None, link: str | None = None
    ) -> int:
        self.calls.append((fitted, photo, link))
        if self.db is not None:
            self.open_scopes.append(self.db.open_scopes)
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
    image_checks: list[str | None]


def _env(
    news: list[News],
    image_ok: bool = True,
    sender: Sender | None = None,
    scores: dict[int, float] | None = None,
    cfg: PublisherConfig = CFG,
) -> Env:
    scores = scores or {}
    posts = {n.id: _post(n.id, scores.get(n.id, 0.9)) for n in news}
    db = Db(news={n.id: n for n in news}, posts=posts)
    sender = sender or Sender()
    sender.db = db
    sink = Sink()
    checks: list[str | None] = []

    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        db.open_scopes += 1
        try:
            yield db
        finally:
            db.open_scopes -= 1

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
        cfg=cfg,
        channel_id=CHANNEL,
        tz=TZ,
    )
    return Env(db, sender, sink, deps, checks)


async def test_successful_publication_one_per_call() -> None:
    env = _env([News(1), News(2)], scores={1: 0.6, 2: 0.8})
    report = await publish_due(NOON, env.deps)
    assert report == PublishReport(news_id=2, message_id=9001)
    assert env.db.news[2].status is NewsStatus.PUBLISHED
    assert env.db.news[1].status is NewsStatus.APPROVED
    [pub] = env.db.publications
    assert pub.status is PublicationStatus.SENT
    assert (pub.message_id, pub.published_at, pub.processed_post_id) == (9001, NOON, 20)
    assert pub.initiated_by is None, "публикация по расписанию не имеет инициатора"
    fitted, image_url, link = env.sender.calls[0]
    assert link is None
    assert fitted.use_photo is True
    assert image_url == "https://cdn.example/a.jpg"
    assert "<b>Заголовок</b>" in fitted.html


def _cfg(decay: float) -> PublisherConfig:
    return CFG.model_copy(update={"score_decay_per_h": decay})


async def test_highest_score_first_without_decay() -> None:
    news = [
        News(1, published_at=NOON),
        News(2, published_at=NOON - timedelta(hours=12)),
        News(3, published_at=NOON - timedelta(hours=1)),
    ]
    env = _env(news, scores={1: 0.6, 2: 0.8, 3: 0.7}, cfg=_cfg(0))
    assert (await publish_due(NOON, env.deps)).news_id == 2


async def test_fresh_post_overtakes_older_with_decay() -> None:
    news = [
        News(1, published_at=NOON - timedelta(hours=12)),
        News(2, published_at=NOON),
    ]
    env = _env(news, scores={1: 0.8, 2: 0.6}, cfg=_cfg(0.02))
    assert (await publish_due(NOON, env.deps)).news_id == 2


async def test_decay_uses_created_at_without_published_at() -> None:
    news = [
        News(1, published_at=NOON - timedelta(hours=12)),
        News(2, created_at=NOON, published_at=None),
    ]
    env = _env(news, scores={1: 0.8, 2: 0.6}, cfg=_cfg(0.02))
    assert (await publish_due(NOON, env.deps)).news_id == 2


async def test_equal_priority_smaller_id_first() -> None:
    env = _env([News(2), News(1)], scores={1: 0.7, 2: 0.7})
    assert (await publish_due(NOON, env.deps)).news_id == 1


async def test_scheduled_post_is_not_claimed_again() -> None:
    """Пост в scheduled уже отправляется: publish_due берёт только approved."""
    news = [News(1), News(2, status=NewsStatus.SCHEDULED)]
    env = _env(news, scores={1: 0.6, 2: 0.9})
    assert (await publish_due(NOON, env.deps)).news_id == 1
    assert env.db.news[2].status is NewsStatus.SCHEDULED


async def test_only_scheduled_left_means_empty_queue() -> None:
    env = _env([News(1, status=NewsStatus.SCHEDULED)])
    assert await publish_due(NOON, env.deps) == PublishReport(skipped="empty")
    assert env.sender.calls == []


async def test_send_happens_without_open_transaction() -> None:
    env = _env([News(1)])
    assert (await publish_due(NOON, env.deps)).message_id == 9001
    assert env.sender.open_scopes == [0], "во время отправки транзакция БД открыта"


async def test_status_scheduled_while_sending() -> None:
    """Между захватом и записью итога пост стоит в scheduled."""
    seen: list[NewsStatus] = []
    env = _env([News(1)])

    class Watching(Sender):
        async def send(
            self, fitted: FittedPost, photo: str | bytes | None, link: str | None = None
        ) -> int:
            seen.append(env.db.news[1].status)
            return await super().send(fitted, photo, link)

    watching = Watching()
    watching.db = env.db
    assert (await publish_due(NOON, replace(env.deps, publisher=watching))).message_id == 9001
    assert seen == [NewsStatus.SCHEDULED]
    assert env.db.news[1].status is NewsStatus.PUBLISHED


class FakeRuntime:
    """Снимок изменяемых параметров публикации."""

    def __init__(self, cfg: PublisherConfig) -> None:
        self.value = RuntimeSnapshot(
            channel=ChannelProfile(**base_section("channel")),
            ai=AiConfig(**base_section("ai")),
            publisher=cfg,
        )
        self.calls = 0

    async def snapshot(self) -> RuntimeSnapshot:
        self.calls += 1
        return self.value


async def test_runtime_interval_applied_without_restart() -> None:
    """Интервал из снимка важнее значения из yaml и действует сразу."""
    env = _env([News(1), News(2)])
    runtime = FakeRuntime(CFG.model_copy(update={"min_interval_min": 120}))
    deps = replace(env.deps, runtime=runtime)

    await publish_due(NOON, deps)
    later = await publish_due(NOON + timedelta(minutes=CFG.min_interval_min), deps)

    assert later.skipped == "slot"
    assert later.next_slot == NOON + timedelta(minutes=120)
    assert runtime.calls >= 2, "снимок берётся на каждом запуске"


async def test_runtime_quiet_hours_applied() -> None:
    env = _env([News(1)])
    runtime = FakeRuntime(CFG.model_copy(update={"quiet_hours": ["11:00", "13:00"]}))
    report = await publish_due(NOON, replace(env.deps, runtime=runtime))
    assert report.skipped == "slot"
    assert env.sender.calls == []


async def test_runtime_decay_changes_order() -> None:
    news = [News(1, published_at=NOON - timedelta(hours=12)), News(2, published_at=NOON)]
    env = _env(news, scores={1: 0.8, 2: 0.6}, cfg=_cfg(0))
    runtime = FakeRuntime(CFG.model_copy(update={"score_decay_per_h": 0.02}))
    assert (await publish_due(NOON, replace(env.deps, runtime=runtime))).news_id == 2


async def test_runtime_stuck_threshold_applied() -> None:
    env = _env([News(1, status=NewsStatus.SCHEDULED, updated_at=NOON - timedelta(minutes=20))])
    runtime = FakeRuntime(CFG.model_copy(update={"stuck_after_min": 60}))
    assert await recover_stuck(NOON, replace(env.deps, runtime=runtime)) == 0
    assert await recover_stuck(NOON, env.deps) == 1


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
    assert env.sender.open_scopes == [0]
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


class Fetcher:
    """Загрузка фото из Telegram-источника."""

    def __init__(
        self, photo: bytes | None = b"photo-bytes", error: Exception | None = None
    ) -> None:
        self.photo = photo
        self.error = error
        self.calls: list[int] = []

    async def fetch(self, news: Any) -> bytes | None:
        self.calls.append(news.source_id)
        if self.error is not None:
            raise self.error
        return self.photo


async def test_telegram_photo_used_without_image_url() -> None:
    """У новости из Telegram нет URL картинки: фото скачивается и уходит файлом."""
    env = _env([News(1, image_url=None)])
    fetcher = Fetcher()
    deps = replace(env.deps, photo_fetcher=fetcher)

    assert (await publish_due(NOON, deps)).message_id == 9001

    fitted, photo, _ = env.sender.calls[0]
    assert photo == b"photo-bytes"
    assert fitted.use_photo is True
    assert fetcher.calls == [1]
    assert env.image_checks == [], "проверка по URL не нужна"


async def test_fetcher_not_called_when_image_url_present() -> None:
    env = _env([News(1)])
    fetcher = Fetcher()
    await publish_due(NOON, replace(env.deps, photo_fetcher=fetcher))

    _, photo, _ = env.sender.calls[0]
    assert photo == "https://cdn.example/a.jpg"
    assert fetcher.calls == []
    assert env.image_checks == ["https://cdn.example/a.jpg"]


async def test_photo_download_error_publishes_text() -> None:
    env = _env([News(1, image_url=None)])
    fetcher = Fetcher(error=RuntimeError("Telethon отключён"))

    with structlog.testing.capture_logs() as logs:
        report = await publish_due(NOON, replace(env.deps, photo_fetcher=fetcher))

    assert report.message_id == 9001
    fitted, photo, _ = env.sender.calls[0]
    assert photo is None
    assert fitted.use_photo is False
    assert [e for e in logs if e["event"] == "publish.tg_photo_failed"]


async def test_no_photo_without_fetcher() -> None:
    env = _env([News(1, image_url=None)])
    assert (await publish_due(NOON, env.deps)).message_id == 9001
    fitted, photo, _ = env.sender.calls[0]
    assert (photo, fitted.use_photo) == (None, False)


async def test_fetcher_without_photo_publishes_text() -> None:
    env = _env([News(1, image_url=None)])
    fetcher = Fetcher(photo=None)
    assert (await publish_due(NOON, replace(env.deps, photo_fetcher=fetcher))).message_id == 9001
    fitted, photo, _ = env.sender.calls[0]
    assert (photo, fitted.use_photo) == (None, False)


async def test_bad_image_published_without_photo() -> None:
    env = _env([News(1)], image_ok=False)
    await publish_due(NOON, env.deps)
    fitted, photo, _ = env.sender.calls[0]
    assert fitted.use_photo is False
    assert photo is None, "неподходящая картинка не уходит в Telegram"
    assert env.image_checks == ["https://cdn.example/a.jpg"]


async def test_empty_queue() -> None:
    env = _env([])
    assert await publish_due(NOON, env.deps) == PublishReport(skipped="empty")


async def test_missing_post_marks_failed() -> None:
    env = _env([News(1)])
    env.db.posts.clear()
    report = await publish_due(NOON, env.deps)
    assert report == PublishReport(news_id=1, error="no_processed_post")
    assert env.db.news[1].status is NewsStatus.FAILED
    assert env.db.reasons[1] == "no_processed_post"
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
