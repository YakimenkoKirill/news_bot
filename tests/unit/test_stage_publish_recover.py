from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.config import PublisherConfig
from app.db.repositories.news import Priority, StaleStatusError, StuckRef
from app.domain.alerts import AlertEvent, AlertKind
from app.domain.enums import NewsStatus
from app.domain.status import ensure_transition
from app.publisher.limits import FittedPost
from app.publisher.media import ImageCheck
from app.publisher.stage_publish import REASON_PUBLISH_UNKNOWN, PublishDeps, recover_stuck
from tests.config_helpers import base_section

CFG = PublisherConfig(**base_section("publisher"))
CHANNEL = -100777
TZ = "Europe/Moscow"
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
STUCK_AFTER = timedelta(minutes=CFG.stuck_after_min)
S = NewsStatus


@dataclass
class News:
    id: int
    status: NewsStatus = S.SCHEDULED
    title: str = "Заголовок"
    sent: bool = False
    updated_at: datetime = NOW - STUCK_AFTER


@dataclass
class Db:
    news: dict[int, News] = field(default_factory=dict)
    reasons: dict[int, str] = field(default_factory=dict)
    stale: set[int] = field(default_factory=set)


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def claim_next(self, statuses: Sequence[NewsStatus], priority: Priority) -> None:
        return None

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        item = self.db.news[news_id]
        if news_id in self.db.stale or item.status is not expected:
            raise StaleStatusError(news_id, expected)
        ensure_transition(expected, new)
        item.status = new
        if reason:
            self.db.reasons[news_id] = reason

    async def list_stuck_scheduled(self, before: datetime) -> list[StuckRef]:
        return [
            StuckRef(news_id=n.id, title=n.title, sent=n.sent)
            for n in sorted(self.db.news.values(), key=lambda n: (n.updated_at, n.id))
            if n.status is S.SCHEDULED and n.updated_at <= before
        ]


class Sender:
    def __init__(self) -> None:
        self.calls: list[FittedPost] = []

    async def send(self, fitted: FittedPost, image_url: str | None, link: str | None = None) -> int:
        self.calls.append(fitted)
        return 9001


class Sink:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    async def emit(self, event: AlertEvent) -> None:
        self.events.append(event)


def _missing(session: Any) -> Any:
    raise AssertionError("восстановление не читает пересказы и источники")


@dataclass
class Env:
    db: Db
    sender: Sender
    sink: Sink
    deps: PublishDeps


def _env(news: list[News], cfg: PublisherConfig = CFG) -> Env:
    db = Db(news={n.id: n for n in news})
    sender, sink = Sender(), Sink()

    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    async def check(url: str | None) -> ImageCheck:
        raise AssertionError("восстановление не проверяет картинки")

    deps = PublishDeps(
        session_scope=scope,
        news_repo=NewsRepo,
        post_repo=_missing,
        source_repo=_missing,
        publication_repo=_missing,
        settings_repo=_missing,
        check_image=check,
        publisher=sender,
        sink=sink,
        cfg=cfg,
        channel_id=CHANNEL,
        tz=TZ,
    )
    return Env(db, sender, sink, deps)


async def test_stuck_without_publication_fails_with_alert() -> None:
    env = _env([News(1, title="Ставка ЦБ")])
    assert await recover_stuck(NOW, env.deps) == 1
    assert env.db.news[1].status is S.FAILED
    assert env.db.reasons[1] == REASON_PUBLISH_UNKNOWN
    [event] = env.sink.events
    assert event.kind is AlertKind.PUBLISH_ERROR
    assert event.key == "publish_error:1"
    assert "Ставка ЦБ" in event.message
    assert "Проверьте канал" in event.message
    assert event.created_at == NOW


async def test_not_resent_automatically() -> None:
    env = _env([News(1)])
    await recover_stuck(NOW, env.deps)
    assert env.sender.calls == []


async def test_stuck_with_publication_becomes_published() -> None:
    env = _env([News(1, sent=True)])
    assert await recover_stuck(NOW, env.deps) == 1
    assert env.db.news[1].status is S.PUBLISHED
    assert env.sink.events == []
    assert 1 not in env.db.reasons


async def test_fresh_scheduled_is_not_touched() -> None:
    env = _env([News(1, updated_at=NOW - STUCK_AFTER + timedelta(minutes=1))])
    assert await recover_stuck(NOW, env.deps) == 0
    assert env.db.news[1].status is S.SCHEDULED
    assert env.sink.events == []


async def test_other_statuses_are_not_touched() -> None:
    env = _env([News(1, status=S.APPROVED), News(2, status=S.PUBLISHED)])
    assert await recover_stuck(NOW, env.deps) == 0
    assert env.db.news[1].status is S.APPROVED
    assert env.db.news[2].status is S.PUBLISHED


async def test_several_stuck_posts() -> None:
    env = _env(
        [
            News(1, updated_at=NOW - timedelta(hours=2)),
            News(2, sent=True, updated_at=NOW - timedelta(hours=1)),
            News(3, updated_at=NOW - STUCK_AFTER),
        ]
    )
    assert await recover_stuck(NOW, env.deps) == 3
    assert [env.db.news[i].status for i in (1, 2, 3)] == [S.FAILED, S.PUBLISHED, S.FAILED]
    assert [event.key for event in env.sink.events] == ["publish_error:1", "publish_error:3"]


async def test_status_changed_by_other_instance_is_skipped() -> None:
    """Второй экземпляр уже разобрал пост: StaleStatusError не роняет задачу."""
    env = _env([News(1), News(2)])
    env.db.stale.add(1)
    with structlog.testing.capture_logs() as logs:
        assert await recover_stuck(NOW, env.deps) == 1
    assert env.db.news[1].status is S.SCHEDULED
    assert env.db.news[2].status is S.FAILED
    assert [event.key for event in env.sink.events] == ["publish_error:2"]
    assert [e for e in logs if e["event"] == "publish.stuck_skipped"]


async def test_threshold_from_config() -> None:
    cfg = CFG.model_copy(update={"stuck_after_min": 60})
    env = _env([News(1, updated_at=NOW - timedelta(minutes=30))], cfg=cfg)
    assert await recover_stuck(NOW, env.deps) == 0
    older = _env([News(1, updated_at=NOW - timedelta(minutes=61))], cfg=cfg)
    assert await recover_stuck(NOW, older.deps) == 1


async def test_nothing_stuck() -> None:
    env = _env([])
    assert await recover_stuck(NOW, env.deps) == 0
    assert env.sink.events == []


def test_real_repository_fits_protocol() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.news import NewsRepository

    deps = _env([]).deps
    assert deps.news_repo is NewsRepo
    assert isinstance(NewsRepository(MagicMock()), NewsRepository)
