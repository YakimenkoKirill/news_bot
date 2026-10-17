import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing
from telethon.tl.types import PeerChannel

from app.collectors.base import RawItem
from app.collectors.telegram.collector import (
    TelegramCollector,
    TelegramDeps,
    channel_username,
)
from app.db.models import Source
from app.domain.enums import SourceType
from app.health import TG_ALIVE_SETTING
from tests.config_helpers import telegram_config

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CONFIG = telegram_config(catch_up_limit=3)
MAX_AGE_H = 24.0
OLD = NOW - timedelta(hours=MAX_AGE_H, minutes=1)


def _source(source_id: int, url: str, enabled: bool = True) -> Source:
    return Source(
        id=source_id, type=SourceType.TG, url=url, name=url, interval_min=1, enabled=enabled
    )


def _msg(
    msg_id: int,
    channel_id: int,
    text: str = "",
    grouped_id: int | None = None,
    photo: bool = False,
    fwd: bool = False,
    date: datetime | None = NOW,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=msg_id,
        message=text,
        entities=[],
        photo=SimpleNamespace(id=msg_id * 10) if photo else None,
        date=date,
        peer_id=PeerChannel(channel_id=channel_id),
        grouped_id=grouped_id,
        fwd_from=object() if fwd else None,
    )


class FakeClient:
    def __init__(self, entities: dict[str, Any], history: dict[int, list[Any]]) -> None:
        self.entities = entities
        self.history = history
        self.iter_calls: list[tuple[int, int, int]] = []
        self.handlers: list[tuple[Any, Any]] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def is_user_authorized(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.connected = False

    async def get_entity(self, username: str) -> Any:
        entity = self.entities[username]
        if isinstance(entity, Exception):
            raise entity
        return entity

    async def iter_messages(self, entity: Any, min_id: int, limit: int) -> AsyncIterator[Any]:
        self.iter_calls.append((entity.id, min_id, limit))
        newest_first = sorted(self.history.get(entity.id, []), key=lambda m: -m.id)
        for message in [m for m in newest_first if m.id > min_id][:limit]:
            yield message

    def add_event_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback: Any) -> None:
        self.handlers = [h for h in self.handlers if h[0] != callback]


class Store:
    def __init__(self, sources: list[Source]) -> None:
        self.sources = sources
        self.news: dict[str, RawItem] = {}
        self.successes: list[tuple[int, int]] = []
        self.errors: list[tuple[int, BaseException]] = []
        self.settings: dict[str, Any] = {}


class FakeSettings:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def set(self, key: str, value: Any) -> None:
        self.store.settings[key] = value


class FakeSources:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def list_enabled(self, type: SourceType | None = None) -> Sequence[Source]:
        return [s for s in self.store.sources if s.enabled and (type is None or s.type == type)]


class FakeNews:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def add_if_new(self, raw: RawItem) -> object | None:
        if raw.external_id in self.store.news:
            return None
        self.store.news[raw.external_id] = raw
        return object()

    async def max_tg_message_id(self, source_id: int) -> int | None:
        ids = [r.raw_meta["msg_id"] for r in self.store.news.values() if r.source_id == source_id]
        return max(ids) if ids else None


class FakeHealth:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def on_success(self, source: Source, items_count: int, now: datetime) -> None:
        self.store.successes.append((source.id, items_count))

    async def on_error(self, source: Source, error: BaseException) -> int:
        self.store.errors.append((source.id, error))
        return 1


class ManualSleep:
    def __init__(self) -> None:
        self.futures: list[asyncio.Future[None]] = []

    async def __call__(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.futures.append(future)
        await future

    async def release(self) -> None:
        for _ in range(10):
            await asyncio.sleep(0)
        for future in self.futures:
            if not future.done():
                future.set_result(None)
        for _ in range(10):
            await asyncio.sleep(0)


def _deps(store: Store) -> TelegramDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Store]:
        yield store

    return TelegramDeps(
        session_scope=scope,
        source_repo=FakeSources,
        news_repo=FakeNews,
        health=FakeHealth,
        settings_repo=FakeSettings,
    )


CHAN_A = SimpleNamespace(id=111, username="chan_a")
CHAN_B = SimpleNamespace(id=222, username="chan_b")


def _collector(
    store: Store,
    client: FakeClient,
    sleep: ManualSleep,
    max_age_h: float = MAX_AGE_H,
) -> TelegramCollector:
    return TelegramCollector(
        client, _deps(store), CONFIG, max_age_h, clock=lambda: NOW, album_sleep=sleep
    )


async def _handler(client: FakeClient) -> Any:
    [(callback, _event)] = client.handlers
    return callback


async def test_catch_up_stops_at_message_older_than_max_age() -> None:
    """Догрузка не уходит глубже filters.max_age_h: старые сообщения не сохраняются."""
    store = Store([_source(1, "https://t.me/chan_a")])
    history = {
        111: [
            _msg(1, 111, text="позавчера", date=OLD),
            _msg(2, 111, text="вчера днём", date=OLD),
            _msg(3, 111, text="свежая", date=NOW - timedelta(hours=2)),
            _msg(4, 111, text="совсем свежая", date=NOW),
        ]
    }
    client = FakeClient({"chan_a": CHAN_A}, history)
    collector = _collector(store, client, ManualSleep())
    with structlog.testing.capture_logs() as logs:
        await collector.start()
    assert sorted(store.news) == ["111:3", "111:4"]
    assert store.successes == [(1, 2)]
    [entry] = [e for e in logs if e["event"] == "telegram.caught_up"]
    assert entry["stopped_by_age"] is True
    assert entry["fetched"] == 2


async def test_fresh_messages_work_as_before() -> None:
    store = Store([_source(1, "chan_a")])
    history = {111: [_msg(i, 111, text=f"новость {i}", date=NOW) for i in (1, 2)]}
    client = FakeClient({"chan_a": CHAN_A}, history)
    collector = _collector(store, client, ManualSleep())
    with structlog.testing.capture_logs() as logs:
        await collector.start()
    assert sorted(store.news) == ["111:1", "111:2"]
    [entry] = [e for e in logs if e["event"] == "telegram.caught_up"]
    assert entry["stopped_by_age"] is False


async def test_all_messages_old_stores_nothing() -> None:
    store = Store([_source(1, "chan_a")])
    history = {111: [_msg(i, 111, text=f"старое {i}", date=OLD) for i in (1, 2, 3)]}
    client = FakeClient({"chan_a": CHAN_A}, history)
    collector = _collector(store, client, ManualSleep())
    await collector.start()
    assert store.news == {}
    assert store.successes == [(1, 0)]


async def test_album_on_cutoff_border_is_stored_whole() -> None:
    """Решение по дате — по самому новому сообщению альбома."""
    store = Store([_source(1, "chan_a")])
    history = {
        111: [
            _msg(5, 111, text="старое", date=OLD),
            _msg(6, 111, grouped_id=9, photo=True, date=OLD),
            _msg(7, 111, text="Альбом", grouped_id=9, photo=True, date=NOW),
        ]
    }
    client = FakeClient({"chan_a": CHAN_A}, history)
    collector = _collector(store, client, ManualSleep())
    await collector.start()
    assert list(store.news) == ["111:6"]
    raw = store.news["111:6"]
    assert raw.text == "Альбом"
    assert raw.raw_meta["photo_ids"] == [60, 70]
    assert "111:5" not in store.news


async def test_message_without_date_is_fresh_with_warning() -> None:
    store = Store([_source(1, "chan_a")])
    history = {111: [_msg(1, 111, text="без даты", date=None)]}
    client = FakeClient({"chan_a": CHAN_A}, history)
    collector = _collector(store, client, ManualSleep())
    with structlog.testing.capture_logs() as logs:
        await collector.start()
    assert list(store.news) == ["111:1"]
    assert [e for e in logs if e["event"] == "telegram.message_without_date"]


async def test_naive_date_compared_as_utc() -> None:
    store = Store([_source(1, "chan_a")])
    history = {111: [_msg(1, 111, text="старое без tz", date=OLD.replace(tzinfo=None))]}
    client = FakeClient({"chan_a": CHAN_A}, history)
    collector = _collector(store, client, ManualSleep())
    await collector.start()
    assert store.news == {}


async def test_max_age_from_argument() -> None:
    store = Store([_source(1, "chan_a")])
    history = {111: [_msg(1, 111, text="три часа назад", date=NOW - timedelta(hours=3))]}
    client = FakeClient({"chan_a": CHAN_A}, history)
    await _collector(store, client, ManualSleep(), max_age_h=1).start()
    assert store.news == {}
    await _collector(store, client, ManualSleep(), max_age_h=4).start()
    assert list(store.news) == ["111:1"]


async def test_catch_up_uses_min_id_and_limit() -> None:
    store = Store([_source(1, "https://t.me/chan_a")])
    history = {111: [_msg(i, 111, text=f"новость {i}") for i in range(1, 11)]}
    client = FakeClient({"chan_a": CHAN_A}, history)
    store.news["111:5"] = RawItem(
        source_id=1,
        external_id="111:5",
        url="https://t.me/chan_a/5",
        title="t",
        text="t",
        raw_meta={"msg_id": 5},
    )
    collector = _collector(store, client, ManualSleep())
    await collector.start()
    assert client.iter_calls == [(111, 5, CONFIG.catch_up_limit)]
    assert set(store.news) == {"111:5", "111:8", "111:9", "111:10"}
    assert store.successes == [(1, 3)]
    assert client.connected


async def test_restart_catches_up_without_duplicates() -> None:
    store = Store([_source(1, "@chan_a")])
    history = {111: [_msg(1, 111, text="a"), _msg(2, 111, text="b")]}
    client = FakeClient({"chan_a": CHAN_A}, history)
    first = _collector(store, client, ManualSleep())
    await first.start()
    await first.stop()
    history[111].append(_msg(3, 111, text="c"))
    second = _collector(store, client, ManualSleep())
    await second.start()
    assert client.iter_calls[-1] == (111, 2, CONFIG.catch_up_limit)
    assert sorted(store.news) == ["111:1", "111:2", "111:3"]


async def test_new_message_handler_stores_news() -> None:
    store = Store([_source(1, "chan_a")])
    client = FakeClient({"chan_a": CHAN_A}, {})
    sleep = ManualSleep()
    collector = _collector(store, client, sleep)
    await collector.start()
    handler = await _handler(client)
    await handler(SimpleNamespace(message=_msg(50, 111, text="Свежая новость")))
    raw = store.news["111:50"]
    assert raw.url == "https://t.me/chan_a/50"
    assert raw.title == "Свежая новость"
    assert store.successes[-1] == (1, 1)


async def test_album_merged_into_one_news() -> None:
    store = Store([_source(1, "chan_a")])
    client = FakeClient({"chan_a": CHAN_A}, {})
    sleep = ManualSleep()
    collector = _collector(store, client, sleep)
    await collector.start()
    handler = await _handler(client)
    await handler(SimpleNamespace(message=_msg(60, 111, grouped_id=9, photo=True)))
    await handler(SimpleNamespace(message=_msg(61, 111, text="Альбом", grouped_id=9, photo=True)))
    await handler(SimpleNamespace(message=_msg(62, 111, grouped_id=9, photo=True)))
    assert store.news == {}
    await sleep.release()
    assert list(store.news) == ["111:60"]
    raw = store.news["111:60"]
    assert raw.text == "Альбом"
    assert raw.raw_meta["photo_ids"] == [600, 610, 620]


async def test_advertisement_and_forwards_skipped() -> None:
    store = Store([_source(1, "chan_a")])
    client = FakeClient({"chan_a": CHAN_A}, {})
    sleep = ManualSleep()
    collector = _collector(store, client, sleep)
    await collector.start()
    handler = await _handler(client)
    await handler(SimpleNamespace(message=_msg(70, 111, text="Купите курс #реклама")))
    await handler(SimpleNamespace(message=_msg(71, 111, text="Репост", fwd=True)))
    await handler(SimpleNamespace(message=_msg(72, 111, grouped_id=5, photo=True)))
    await handler(SimpleNamespace(message=_msg(73, 111, text="erid: 123", grouped_id=5)))
    await sleep.release()
    await handler(SimpleNamespace(message=_msg(74, 111, text="Обычная новость")))
    assert list(store.news) == ["111:74"]


async def test_reload_sources_reregisters_handler() -> None:
    store = Store([_source(1, "chan_a")])
    client = FakeClient({"chan_a": CHAN_A, "chan_b": CHAN_B}, {})
    collector = _collector(store, client, ManualSleep())
    await collector.start()
    assert set(collector.channels) == {111}
    store.sources.append(_source(2, "https://t.me/chan_b"))
    store.sources[0].enabled = False
    await collector.reload_sources()
    assert len(client.handlers) == 1
    assert set(collector.channels) == {222}
    handler = await _handler(client)
    await handler(SimpleNamespace(message=_msg(1, 111, text="от отключённого")))
    await handler(SimpleNamespace(message=_msg(2, 222, text="от нового")))
    assert list(store.news) == ["222:2"]


async def test_unresolved_channel_does_not_block_others() -> None:
    store = Store([_source(1, "chan_a"), _source(2, "missing")])
    client = FakeClient(
        {"chan_a": CHAN_A, "missing": ValueError("No user has 'missing' as username")},
        {111: [_msg(1, 111, text="ok")]},
    )
    collector = _collector(store, client, ManualSleep())
    await collector.start()
    assert set(collector.channels) == {111}
    [(source_id, error)] = store.errors
    assert source_id == 2
    assert "missing" in str(error)
    assert "111:1" in store.news


async def test_stop_flushes_albums_and_disconnects() -> None:
    store = Store([_source(1, "chan_a")])
    client = FakeClient({"chan_a": CHAN_A}, {})
    collector = _collector(store, client, ManualSleep())
    await collector.start()
    handler = await _handler(client)
    await handler(SimpleNamespace(message=_msg(80, 111, text="Незавершённый альбом", grouped_id=3)))
    await collector.stop()
    assert "111:80" in store.news
    assert client.handlers == []
    assert not client.connected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://t.me/chan_a", "chan_a"),
        ("t.me/chan_a/123", "chan_a"),
        ("@chan_a", "chan_a"),
        ("chan_a", "chan_a"),
        ("https://t.me/chan_a?start=1", "chan_a"),
    ],
)
def test_channel_username(url: str, expected: str) -> None:
    assert channel_username(_source(1, url)) == expected


def test_real_components_fit_protocols() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.collectors.health import SourceHealthTracker
    from app.db.repositories.news import NewsRepository
    from app.db.repositories.sources import SourceRepository
    from app.domain.alerts import AlertEvent
    from tests.config_helpers import collectors_config

    class Sink:
        async def emit(self, event: AlertEvent) -> None:
            return None

    def health(session: AsyncSession) -> SourceHealthTracker:
        return SourceHealthTracker(SourceRepository(session), Sink(), collectors_config())

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        raise AssertionError("не вызывается")
        yield

    deps = TelegramDeps(
        session_scope=scope,
        source_repo=SourceRepository,
        news_repo=NewsRepository,
        health=health,
    )
    assert deps.news_repo is NewsRepository


async def test_mark_alive_writes_timestamp() -> None:
    store = Store([_source(1, "chan_a")])
    client = FakeClient({"chan_a": CHAN_A}, {})
    collector = _collector(store, client, ManualSleep())
    await collector.start()

    assert await collector.mark_alive(NOW) is True
    assert store.settings[TG_ALIVE_SETTING] == NOW.isoformat()


async def test_mark_alive_skips_when_disconnected() -> None:
    """Telethon отключился — отметка не обновляется, и проверка живости это увидит."""
    store = Store([_source(1, "chan_a")])
    client = FakeClient({"chan_a": CHAN_A}, {})
    collector = _collector(store, client, ManualSleep())
    await collector.start()
    await collector.stop()

    with structlog.testing.capture_logs() as logs:
        assert await collector.mark_alive(NOW) is False

    assert TG_ALIVE_SETTING not in store.settings
    assert [e for e in logs if e["event"] == "telegram.not_connected"]
