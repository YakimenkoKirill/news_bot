from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing
from telethon.errors import FloodWaitError

from app.collectors.base import TemporaryCollectorError
from app.collectors.telegram.collector import (
    STATS_BATCH,
    PostStats,
    TelegramCollector,
    TelegramDeps,
)
from app.config import StatsConfig
from app.db.repositories.posts import PendingStats
from app.domain.alerts import AlertEvent, AlertKind
from app.stats import StatsDeps, StatsReport, collect_post_stats
from tests.config_helpers import base_section, telegram_config

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CHANNEL = -1001234567890
CFG = StatsConfig(**base_section("stats"))
TG_CONFIG = telegram_config()
MAX_AGE = timedelta(hours=CFG.max_age_h)


def _message(message_id: int, views: int = 100, forwards: int = 5, reactions: int = 3) -> Any:
    results = [SimpleNamespace(count=reactions)] if reactions else []
    return SimpleNamespace(
        id=message_id,
        views=views,
        forwards=forwards,
        reactions=SimpleNamespace(results=results) if results else None,
    )


class StatsClient:
    """Клиент Telethon: отдаёт сообщения по id, запоминает пачки."""

    def __init__(self, messages: dict[int, Any], entity_error: Exception | None = None) -> None:
        self.messages = messages
        self.entity_error = entity_error
        self.batches: list[list[int]] = []
        self.flood: list[int] = []

    async def get_entity(self, channel: int) -> Any:
        if self.entity_error is not None:
            raise self.entity_error
        return SimpleNamespace(id=channel)

    async def get_messages(self, entity: Any, ids: list[int]) -> list[Any]:
        self.batches.append(list(ids))
        if self.flood:
            seconds = self.flood.pop(0)
            raise FloodWaitError(request=None, capture=seconds)
        return [self.messages.get(message_id) for message_id in ids]

    async def connect(self) -> None:
        return None

    async def is_user_authorized(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    def add_event_handler(self, callback: Any, event: Any) -> None:
        return None

    def remove_event_handler(self, callback: Any) -> None:
        return None


def _collector(client: StatsClient) -> TelegramCollector:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Any]:
        yield object()

    deps = TelegramDeps(
        session_scope=scope,
        source_repo=lambda session: SimpleNamespace(),
        news_repo=lambda session: SimpleNamespace(),
        health=lambda session: SimpleNamespace(),
    )
    return TelegramCollector(client, deps, TG_CONFIG, 24.0, clock=lambda: NOW)


async def no_sleep(seconds: float) -> None:
    return None


async def test_fetch_collects_views_forwards_reactions() -> None:
    client = StatsClient({7: _message(7, views=1200, forwards=30, reactions=12)})
    stats = await _collector(client).fetch_post_stats(CHANNEL, [7])
    assert stats == {7: PostStats(views=1200, forwards=30, reactions=12)}
    assert client.batches == [[7]]


async def test_fetch_sums_all_reaction_kinds() -> None:
    message = _message(7)
    message.reactions = SimpleNamespace(
        results=[SimpleNamespace(count=4), SimpleNamespace(count=6)]
    )
    stats = await _collector(StatsClient({7: message})).fetch_post_stats(CHANNEL, [7])
    assert stats[7].reactions == 10


async def test_reactions_disabled_gives_zero() -> None:
    message = _message(7, reactions=0)
    assert message.reactions is None
    stats = await _collector(StatsClient({7: message})).fetch_post_stats(CHANNEL, [7])
    assert stats[7].reactions == 0
    assert stats[7].views == 100


async def test_missing_views_and_forwards_are_zero() -> None:
    message = SimpleNamespace(id=7, views=None, forwards=None, reactions=None)
    stats = await _collector(StatsClient({7: message})).fetch_post_stats(CHANNEL, [7])
    assert stats == {7: PostStats(views=0, forwards=0, reactions=0)}


async def test_deleted_message_absent_in_result() -> None:
    client = StatsClient({7: _message(7)})
    stats = await _collector(client).fetch_post_stats(CHANNEL, [7, 8])
    assert set(stats) == {7}


async def test_requests_are_batched() -> None:
    ids = list(range(1, STATS_BATCH + 31))
    client = StatsClient({message_id: _message(message_id) for message_id in ids})
    stats = await _collector(client).fetch_post_stats(CHANNEL, ids)
    assert [len(batch) for batch in client.batches] == [STATS_BATCH, 30]
    assert len(stats) == len(ids)


async def test_empty_request_does_not_touch_client() -> None:
    client = StatsClient({})
    assert await _collector(client).fetch_post_stats(CHANNEL, []) == {}
    assert client.batches == []


async def test_flood_wait_is_retried() -> None:
    """FloodWait пережидается, как в остальном сборщике: запрос повторяется."""
    client = StatsClient({7: _message(7)})
    client.flood = [1]
    stats = await _collector(client).fetch_post_stats(CHANNEL, [7])
    assert set(stats) == {7}
    assert len(client.batches) == 2


async def test_unavailable_channel_raises() -> None:
    client = StatsClient({}, entity_error=RuntimeError("Cannot find any entity"))
    with pytest.raises(RuntimeError, match="entity"):
        await _collector(client).fetch_post_stats(CHANNEL, [7])


async def test_long_flood_wait_becomes_temporary_error() -> None:
    client = StatsClient({7: _message(7)})
    client.flood = [int(TG_CONFIG.flood_wait_max_s) + 10]
    with pytest.raises(TemporaryCollectorError):
        await _collector(client).fetch_post_stats(CHANNEL, [7])


@dataclass
class Db:
    pending: list[PendingStats] = field(default_factory=list)
    saved: list[tuple[int, int | None, int | None, int | None, datetime]] = field(
        default_factory=list
    )
    given_up: list[tuple[datetime, datetime]] = field(default_factory=list)
    windows: list[tuple[datetime, datetime]] = field(default_factory=list)
    give_up_count: int = 0


class PublicationRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def list_for_stats(
        self, channel_id: int, since: datetime, until: datetime, limit: int
    ) -> list[PendingStats]:
        assert channel_id == CHANNEL
        self.db.windows.append((since, until))
        return self.db.pending[:limit]

    async def save_stats(
        self,
        publication_id: int,
        at: datetime,
        *,
        views: int | None = None,
        forwards: int | None = None,
        reactions: int | None = None,
    ) -> bool:
        self.db.saved.append((publication_id, views, forwards, reactions, at))
        return True

    async def give_up_stats(self, channel_id: int, before: datetime, at: datetime) -> int:
        self.db.given_up.append((before, at))
        return self.db.give_up_count


class Fetcher:
    def __init__(self, stats: dict[int, PostStats], error: Exception | None = None) -> None:
        self.stats = stats
        self.error = error
        self.calls: list[list[int]] = []

    async def fetch_post_stats(
        self, channel_id: int, message_ids: Sequence[int]
    ) -> dict[int, PostStats]:
        self.calls.append(list(message_ids))
        if self.error is not None:
            raise self.error
        return {
            message_id: self.stats[message_id]
            for message_id in message_ids
            if message_id in self.stats
        }


class Sink:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    async def emit(self, event: AlertEvent) -> None:
        self.events.append(event)


def _deps(db: Db, fetcher: Fetcher, sink: Sink) -> StatsDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    return StatsDeps(
        session_scope=scope,
        publication_repo=PublicationRepo,
        fetcher=fetcher,
        sink=sink,
        cfg=CFG,
        channel_id=CHANNEL,
    )


async def test_job_saves_collected_stats() -> None:
    db = Db(pending=[PendingStats(publication_id=1, message_id=10)])
    fetcher = Fetcher({10: PostStats(views=900, forwards=12, reactions=4)})
    sink = Sink()

    report = await collect_post_stats(NOW, _deps(db, fetcher, sink))

    assert report == StatsReport(requested=1, updated=1, missing=0, given_up=0)
    assert db.saved == [(1, 900, 12, 4, NOW)]
    assert sink.events == []


async def test_job_window_from_config() -> None:
    db = Db(pending=[])
    await collect_post_stats(NOW, _deps(db, Fetcher({}), Sink()))
    since, until = db.windows[0]
    assert since == NOW - timedelta(hours=CFG.max_age_h)
    assert until == NOW - timedelta(hours=CFG.delay_h)
    assert db.given_up == [(since, NOW)]


async def test_deleted_post_gets_stats_at_without_values() -> None:
    db = Db(pending=[PendingStats(publication_id=2, message_id=11)])
    report = await collect_post_stats(NOW, _deps(db, Fetcher({}), Sink()))
    assert report.missing == 1
    assert db.saved == [(2, None, None, None, NOW)]


async def test_old_posts_are_given_up() -> None:
    db = Db(pending=[], give_up_count=3)
    with structlog.testing.capture_logs() as logs:
        report = await collect_post_stats(NOW, _deps(db, Fetcher({}), Sink()))
    assert report.given_up == 3
    assert [e for e in logs if e["event"] == "stats.given_up"]


async def test_unavailable_channel_creates_incident_and_does_not_fail() -> None:
    db = Db(pending=[PendingStats(publication_id=3, message_id=12)])
    sink = Sink()
    fetcher = Fetcher({}, error=RuntimeError("channel private"))

    with structlog.testing.capture_logs() as logs:
        report = await collect_post_stats(NOW, _deps(db, fetcher, sink))

    assert report.failed is True
    assert report.updated == 0
    assert db.saved == []
    [event] = sink.events
    assert event.kind is AlertKind.STATS_UNAVAILABLE
    assert event.key == f"stats_unavailable:{CHANNEL}"
    assert "Telethon" in event.message
    assert [e for e in logs if e["event"] == "stats.channel_unavailable"]


async def test_collected_posts_are_not_requested_again() -> None:
    db = Db(pending=[PendingStats(publication_id=4, message_id=13)])
    fetcher = Fetcher({13: PostStats(views=10, forwards=0, reactions=0)})
    await collect_post_stats(NOW, _deps(db, fetcher, Sink()))
    db.pending = []
    second = await collect_post_stats(NOW, _deps(db, fetcher, Sink()))
    assert second.requested == 0
    assert fetcher.calls == [[13]]


def test_real_components_fit_protocols() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.posts import PublicationRepository

    deps = StatsDeps(
        session_scope=MagicMock(),
        publication_repo=PublicationRepository,
        fetcher=_collector(StatsClient({})),
        sink=Sink(),
        cfg=CFG,
        channel_id=CHANNEL,
    )
    assert deps.publication_repo is PublicationRepository
    assert isinstance(PublicationRepository(MagicMock()), PublicationRepository)
