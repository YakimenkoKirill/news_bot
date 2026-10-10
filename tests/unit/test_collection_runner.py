import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from app.collectors.base import (
    BaseCollector,
    FetchResult,
    PermanentCollectorError,
    RawItem,
    TemporaryCollectorError,
)
from app.collectors.runner import CollectionDeps, run_collection_cycle
from app.db.models import Source
from app.domain.enums import SourceType

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _source(source_id: int, type: SourceType = SourceType.RSS) -> Source:
    return Source(
        id=source_id,
        type=type,
        url=f"https://s{source_id}.example/",
        name=f"S{source_id}",
        interval_min=10,
        fetch_state={"etag": f"old-{source_id}"},
    )


def _raw(source_id: int, ext: str) -> RawItem:
    return RawItem(
        source_id=source_id,
        external_id=ext,
        url=f"https://s{source_id}.example/{ext}",
        title=ext,
        text="text",
    )


class FakeStore:
    """Общее состояние фейковой «БД»."""

    def __init__(self, due: list[Source], existing: set[tuple[int, str]] | None = None) -> None:
        self.due = due
        self.news: set[tuple[int, str]] = set(existing or set())
        self.checked: dict[int, dict[str, Any]] = {}
        self.successes: list[tuple[int, int]] = []
        self.errors: list[tuple[int, BaseException]] = []
        self.sessions = 0
        self.committed = 0


class FakeSourceRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def list_due(self, now: datetime) -> Sequence[Source]:
        return self.store.due

    async def mark_checked(self, source_id: int, at: datetime, fetch_state: dict[str, Any]) -> bool:
        self.store.checked[source_id] = fetch_state
        return True


class FakeNewsRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def add_if_new(self, raw: RawItem) -> object | None:
        key = (raw.source_id, raw.external_id)
        if key in self.store.news:
            return None
        self.store.news.add(key)
        return object()


class FakeHealth:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def on_success(self, source: Source, items_count: int, now: datetime) -> None:
        self.store.successes.append((source.id, items_count))

    async def on_error(self, source: Source, error: BaseException) -> int:
        self.store.errors.append((source.id, error))
        return 1


class FakeCollector(BaseCollector):
    def __init__(self, results: dict[int, list[RawItem] | Exception]) -> None:
        self.results = results
        self.active = 0
        self.max_active = 0

    async def fetch(self, source: Source) -> list[RawItem]:
        return (await self.fetch_result(source)).items

    async def fetch_result(self, source: Source) -> FetchResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            result = self.results.get(source.id, [])
            if isinstance(result, Exception):
                raise result
            return FetchResult(items=result, fetch_state={"etag": f"new-{source.id}"})
        finally:
            self.active -= 1


def _deps(
    store: FakeStore, collectors: dict[SourceType, BaseCollector], parallel: int = 5
) -> CollectionDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[FakeStore]:
        store.sessions += 1
        yield store
        store.committed += 1

    return CollectionDeps(
        session_scope=scope,
        source_repo=FakeSourceRepo,
        news_repo=FakeNewsRepo,
        health=FakeHealth,
        collectors=collectors,
        max_parallel_sources=parallel,
    )


async def test_collects_all_due_sources() -> None:
    store = FakeStore([_source(1), _source(2, SourceType.WEB)], existing={(1, "old")})
    rss = FakeCollector({1: [_raw(1, "a"), _raw(1, "b"), _raw(1, "old")]})
    web = FakeCollector({2: [_raw(2, "x")]})
    report = await run_collection_cycle(
        NOW, _deps(store, {SourceType.RSS: rss, SourceType.WEB: web})
    )
    assert report.checked == 2
    assert report.new_items == 3
    assert report.errors == 0
    assert report.skipped == 0
    assert store.news >= {(1, "a"), (1, "b"), (2, "x")}
    assert store.checked == {1: {"etag": "new-1"}, 2: {"etag": "new-2"}}
    assert sorted(store.successes) == [(1, 2), (2, 1)]


async def test_one_source_error_does_not_stop_others() -> None:
    store = FakeStore([_source(1), _source(2), _source(3)])
    rss = FakeCollector(
        {
            1: TemporaryCollectorError("timeout"),
            2: [_raw(2, "a")],
            3: PermanentCollectorError("gone"),
        }
    )
    report = await run_collection_cycle(NOW, _deps(store, {SourceType.RSS: rss}))
    assert report.checked == 3
    assert report.new_items == 1
    assert report.errors == 2
    assert sorted(source_id for source_id, _ in store.errors) == [1, 3]
    assert store.successes == [(2, 1)]
    assert store.checked[1] == {"etag": "old-1"}
    assert store.checked[2] == {"etag": "new-2"}


async def test_unexpected_exception_counted_as_source_error() -> None:
    store = FakeStore([_source(1), _source(2)])
    rss = FakeCollector({1: RuntimeError("bug"), 2: [_raw(2, "a")]})
    report = await run_collection_cycle(NOW, _deps(store, {SourceType.RSS: rss}))
    assert report.errors == 1
    assert report.new_items == 1


async def test_unknown_source_type_is_source_error() -> None:
    store = FakeStore([_source(1, SourceType.WEB), _source(2)])
    rss = FakeCollector({2: [_raw(2, "a")]})
    report = await run_collection_cycle(NOW, _deps(store, {SourceType.RSS: rss}))
    assert report.checked == 2
    assert report.errors == 1
    [(source_id, error)] = store.errors
    assert source_id == 1
    assert "нет сборщика" in str(error)


async def test_telegram_sources_skipped() -> None:
    store = FakeStore([_source(1, SourceType.TG), _source(2)])
    rss = FakeCollector({2: [_raw(2, "a")]})
    report = await run_collection_cycle(NOW, _deps(store, {SourceType.RSS: rss}))
    assert report.skipped == 1
    assert report.checked == 1
    assert 1 not in store.checked
    assert store.errors == []


@pytest.mark.parametrize("parallel", [1, 2, 3])
async def test_parallel_limit(parallel: int) -> None:
    store = FakeStore([_source(i) for i in range(1, 8)])
    rss = FakeCollector({})
    report = await run_collection_cycle(NOW, _deps(store, {SourceType.RSS: rss}, parallel))
    assert report.checked == 7
    assert rss.max_active == parallel


async def test_each_source_uses_own_session() -> None:
    store = FakeStore([_source(1), _source(2), _source(3)])
    rss = FakeCollector({2: TemporaryCollectorError("x")})
    await run_collection_cycle(NOW, _deps(store, {SourceType.RSS: rss}))
    assert store.sessions == 1 + 3
    assert store.committed == store.sessions


async def test_empty_cycle() -> None:
    store = FakeStore([])
    report = await run_collection_cycle(NOW, _deps(store, {}))
    assert report.checked == report.new_items == report.errors == report.skipped == 0


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

    deps = CollectionDeps(
        session_scope=scope,
        source_repo=SourceRepository,
        news_repo=NewsRepository,
        health=health,
        collectors={},
        max_parallel_sources=1,
    )
    assert deps.max_parallel_sources == 1
