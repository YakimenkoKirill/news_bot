import copy
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.config import DedupConfig, FiltersConfig
from app.domain.enums import KeywordType, NewsStatus
from app.domain.status import ensure_transition
from app.pipeline.dedup import from_db, hamming
from app.pipeline.stage_normalize import NormalizeDeps, StageReport, process_new_batch
from tests.config_helpers import base_section, normalize_config

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
FILTERS = FiltersConfig(**{**base_section("filters"), "allowed_languages": ["ru"]})
DEDUP = DedupConfig(**base_section("dedup"))

TEXT = (
    "Совет директоров Банка России на заседании в пятницу принял решение сохранить "
    "ключевую ставку. Регулятор отметил, что инфляция замедляется."
)


@dataclass
class Row:
    id: int
    url: str
    title: str
    raw_text: str
    published_at: datetime | None = NOW - timedelta(hours=1)
    created_at: datetime = NOW - timedelta(minutes=30)
    status: NewsStatus = NewsStatus.NEW
    reject_reason: str | None = None
    clean_text: str | None = None
    canonical_url: str | None = None
    language: str | None = None
    content_hash: str | None = None
    simhash: int | None = None
    cluster_id: int | None = None


@dataclass
class Db:
    rows: dict[int, Row] = field(default_factory=dict)
    keywords: dict[KeywordType, list[str]] = field(default_factory=dict)
    fail_update_for: int | None = None


class FakeNewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def claim_batch(self, status: NewsStatus, limit: int) -> Sequence[Row]:
        rows = sorted((r for r in self.db.rows.values() if r.status == status), key=lambda r: r.id)
        return rows[:limit]

    async def update_fields(self, news_id: int, **fields: Any) -> bool:
        if news_id == self.db.fail_update_for and "cluster_id" in fields:
            raise RuntimeError("db failure")
        row = self.db.rows[news_id]
        for key, value in fields.items():
            setattr(row, key, value)
        return True

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        row = self.db.rows[news_id]
        assert row.status == expected
        ensure_transition(expected, new)
        row.status = new
        if reason is not None:
            row.reject_reason = reason

    def _window(self, since: datetime, exclude_id: int | None) -> list[Row]:
        return sorted(
            (r for r in self.db.rows.values() if r.created_at >= since and r.id != exclude_id),
            key=lambda r: r.id,
        )

    async def find_by_url(
        self, canonical_url: str, since: datetime, exclude_id: int | None = None
    ) -> Row | None:
        rows = self._window(since, exclude_id)
        return next((r for r in rows if r.canonical_url == canonical_url), None)

    async def find_by_hash(
        self, content_hash: str, since: datetime, exclude_id: int | None = None
    ) -> Row | None:
        rows = self._window(since, exclude_id)
        return next((r for r in rows if r.content_hash == content_hash), None)

    async def recent_simhashes(self, since: datetime) -> list[tuple[int, int, int | None]]:
        rows = self._window(since, None)
        return [(r.id, r.simhash, r.cluster_id) for r in rows if r.simhash is not None]


@dataclass
class Word:
    word: str


class FakeKeywords:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def list_by_type(self, type: KeywordType) -> Sequence[Word]:
        return [Word(w) for w in self.db.keywords.get(type, [])]


def _deps(db: Db, batch_size: int = 20) -> NormalizeDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    @asynccontextmanager
    async def savepoint(session: Db) -> AsyncIterator[None]:
        snapshot = copy.deepcopy(session.rows)
        try:
            yield
        except BaseException:
            session.rows.clear()
            session.rows.update(snapshot)
            raise

    return NormalizeDeps(
        session_scope=scope,
        news_repo=FakeNewsRepo,
        keyword_repo=FakeKeywords,
        savepoint=savepoint,
        normalize=normalize_config(),
        filters=FILTERS,
        dedup=DEDUP,
        batch_size=batch_size,
    )


def _row(row_id: int, **fields: Any) -> Row:
    base: dict[str, Any] = {
        "url": f"https://example.com/news/{row_id}?utm_source=tg",
        "title": "Центробанк сохранил ключевую ставку",
        "raw_text": f"<p>{TEXT}</p>",
    }
    base.update(fields)
    return Row(id=row_id, **base)


async def test_unique_news_normalized_with_own_cluster() -> None:
    db = Db({1: _row(1)})
    report = await process_new_batch(NOW, _deps(db))
    assert report == StageReport(claimed=1, normalized=1)
    row = db.rows[1]
    assert row.status is NewsStatus.NORMALIZED
    assert row.canonical_url == "https://example.com/news/1"
    assert row.clean_text == TEXT
    assert row.language == "ru"
    assert row.content_hash is not None
    assert row.simhash is not None
    assert row.cluster_id == 1


async def test_filtered_with_reason() -> None:
    db = Db(
        {
            1: _row(1, published_at=NOW - timedelta(days=3)),
            2: _row(2, title="Fresh news", raw_text="English only text here"),
        }
    )
    report = await process_new_batch(NOW, _deps(db))
    assert report.filtered == 2
    assert db.rows[1].status is NewsStatus.FILTERED
    assert db.rows[1].reject_reason == "too_old"
    assert db.rows[2].reject_reason == "language:en"


async def test_stop_words_from_keyword_repository() -> None:
    db = Db({1: _row(1)}, keywords={KeywordType.EXCLUDE: ["инфляция"]})
    await process_new_batch(NOW, _deps(db))
    assert db.rows[1].status is NewsStatus.FILTERED
    assert db.rows[1].reject_reason == "stop_word:инфляция"


async def test_duplicate_of_earlier_news_in_same_batch() -> None:
    db = Db({1: _row(1), 2: _row(2, url="https://example.com/news/1?utm_medium=x")})
    report = await process_new_batch(NOW, _deps(db))
    assert report == StageReport(claimed=2, normalized=1, duplicates=1)
    duplicate = db.rows[2]
    assert duplicate.status is NewsStatus.DUPLICATE
    assert duplicate.reject_reason == "duplicate:url:1"
    assert duplicate.cluster_id == 1


async def test_duplicate_by_hash_joins_existing_cluster() -> None:
    db = Db({1: _row(1)})
    await process_new_batch(NOW, _deps(db))
    db.rows[1].status = NewsStatus.PUBLISHED
    db.rows[1].cluster_id = 77
    db.rows[2] = _row(2, url="https://other.example/copy")
    report = await process_new_batch(NOW, _deps(db))
    assert report == StageReport(claimed=1, duplicates=1)
    assert db.rows[2].reject_reason == "duplicate:hash:1"
    assert db.rows[2].cluster_id == 77


async def test_one_item_error_does_not_break_batch() -> None:
    db = Db({1: _row(1, url="not-a-url"), 2: _row(2)})
    report = await process_new_batch(NOW, _deps(db))
    assert report == StageReport(claimed=2, normalized=1, filtered=1, errors=1)
    assert db.rows[1].status is NewsStatus.FILTERED
    assert db.rows[1].reject_reason == "normalize_error"
    assert db.rows[2].status is NewsStatus.NORMALIZED


async def test_failed_item_changes_rolled_back() -> None:
    db = Db({1: _row(1)}, fail_update_for=1)
    report = await process_new_batch(NOW, _deps(db))
    assert report.errors == 1
    row = db.rows[1]
    assert row.status is NewsStatus.FILTERED
    assert row.reject_reason == "normalize_error"
    assert row.canonical_url is None
    assert row.simhash is None


async def test_empty_queue() -> None:
    assert await process_new_batch(NOW, _deps(Db())) == StageReport()


async def test_batch_size_limit_and_other_statuses_ignored() -> None:
    db = Db(
        {
            i: _row(i, url=f"https://e{i}.example/a", raw_text=f"Новость номер {i} {TEXT}")
            for i in range(1, 6)
        }
    )
    db.rows[6] = _row(6, status=NewsStatus.NORMALIZED)
    report = await process_new_batch(NOW, _deps(db, batch_size=3))
    assert report.claimed == 3
    assert [r.status for r in db.rows.values()].count(NewsStatus.NEW) == 2


def test_simhash_stored_as_signed_bigint() -> None:
    assert hamming(from_db(-1), 2**64 - 1) == 0


@pytest.mark.parametrize("batch_size", [1, 20])
async def test_all_claimed_get_final_status(batch_size: int) -> None:
    db = Db({1: _row(1), 2: _row(2, url="bad"), 3: _row(3, published_at=NOW - timedelta(days=9))})
    for _ in range(3):
        await process_new_batch(NOW, _deps(db, batch_size))
    assert all(r.status is not NewsStatus.NEW for r in db.rows.values())
    assert all(r.reject_reason for r in db.rows.values() if r.status is NewsStatus.FILTERED)


def test_real_repositories_fit_protocols() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.repositories.misc import KeywordRepository
    from app.db.repositories.news import NewsRepository

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        raise AssertionError("не вызывается")
        yield

    def savepoint(session: AsyncSession) -> Any:
        return session.begin_nested()

    deps = NormalizeDeps(
        session_scope=scope,
        news_repo=NewsRepository,
        keyword_repo=KeywordRepository,
        savepoint=savepoint,
        normalize=normalize_config(),
        filters=FILTERS,
        dedup=DEDUP,
        batch_size=1,
    )
    assert deps.batch_size == 1
