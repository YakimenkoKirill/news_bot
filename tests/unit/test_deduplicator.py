from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.config import DedupConfig
from app.pipeline.dedup import DedupLevel, Deduplicator, DedupResult, to_db
from tests.config_helpers import base_section

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CFG = DedupConfig(**{**base_section("dedup"), "simhash_max_distance": 3, "window_h": 48})
ITEM_ID = 100
BASE_HASH = 0b1111_0000


@dataclass
class Stored:
    id: int
    created_at: datetime
    canonical_url: str = ""
    content_hash: str = ""
    simhash: int | None = None
    cluster_id: int | None = None


@dataclass
class Fields:
    canonical_url: str = "https://example.com/news/1"
    content_hash: str = "hash-1"


@dataclass
class FakeRepo:
    items: list[Stored] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def _window(self, since: datetime, exclude_id: int | None) -> list[Stored]:
        return sorted(
            (i for i in self.items if i.created_at >= since and i.id != exclude_id),
            key=lambda i: i.id,
        )

    async def find_by_url(
        self, canonical_url: str, since: datetime, exclude_id: int | None = None
    ) -> Stored | None:
        self.calls.append("url")
        return next(
            (i for i in self._window(since, exclude_id) if i.canonical_url == canonical_url), None
        )

    async def find_by_hash(
        self, content_hash: str, since: datetime, exclude_id: int | None = None
    ) -> Stored | None:
        self.calls.append("hash")
        return next(
            (i for i in self._window(since, exclude_id) if i.content_hash == content_hash), None
        )

    async def recent_simhashes(self, since: datetime) -> list[tuple[int, int, int | None]]:
        self.calls.append("simhash")
        return [
            (i.id, to_db(i.simhash), i.cluster_id)
            for i in self._window(since, None)
            if i.simhash is not None
        ]


def _recent(hours: float = 1) -> datetime:
    return NOW - timedelta(hours=hours)


async def _check(
    repo: FakeRepo, fields: Fields | None = None, simhash: int = BASE_HASH
) -> DedupResult:
    return await Deduplicator(repo, CFG).check(ITEM_ID, fields or Fields(), simhash, NOW)


async def test_unique_news_gets_own_cluster() -> None:
    repo = FakeRepo([Stored(1, _recent(), simhash=BASE_HASH ^ 0b1111_1111_0000_0000)])
    assert await _check(repo) == DedupResult(
        is_duplicate=False, duplicate_of=None, cluster_id=ITEM_ID, level=None
    )
    assert repo.calls == ["url", "hash", "simhash"]


async def test_url_level() -> None:
    repo = FakeRepo([Stored(5, _recent(), canonical_url=Fields().canonical_url, cluster_id=2)])
    result = await _check(repo)
    assert result == DedupResult(True, 5, 2, DedupLevel.URL)
    assert repo.calls == ["url"]


async def test_hash_level_cluster_defaults_to_found_id() -> None:
    repo = FakeRepo([Stored(7, _recent(), content_hash=Fields().content_hash)])
    assert await _check(repo) == DedupResult(True, 7, 7, DedupLevel.HASH)


async def test_simhash_level() -> None:
    repo = FakeRepo([Stored(9, _recent(), simhash=BASE_HASH ^ 0b111, cluster_id=4)])
    assert await _check(repo) == DedupResult(True, 9, 4, DedupLevel.SIMHASH)


async def test_simhash_beyond_threshold_is_unique() -> None:
    repo = FakeRepo([Stored(9, _recent(), simhash=BASE_HASH ^ 0b1111)])
    assert not (await _check(repo)).is_duplicate


async def test_level_priority() -> None:
    fields = Fields()
    repo = FakeRepo(
        [
            Stored(1, _recent(), simhash=BASE_HASH),
            Stored(2, _recent(), content_hash=fields.content_hash),
            Stored(3, _recent(), canonical_url=fields.canonical_url),
        ]
    )
    assert (await _check(repo, fields)).level is DedupLevel.URL
    repo.items.pop()
    assert (await _check(repo, fields)).level is DedupLevel.HASH
    repo.items.pop()
    assert (await _check(repo, fields)).level is DedupLevel.SIMHASH


async def test_nearest_simhash_wins_then_earlier() -> None:
    repo = FakeRepo(
        [
            Stored(10, _recent(), simhash=BASE_HASH ^ 0b111),
            Stored(11, _recent(), simhash=BASE_HASH ^ 0b1),
            Stored(12, _recent(), simhash=BASE_HASH ^ 0b10),
        ]
    )
    assert (await _check(repo)).duplicate_of == 11
    repo.items = [
        Stored(21, _recent(), simhash=BASE_HASH ^ 0b100),
        Stored(20, _recent(), simhash=BASE_HASH ^ 0b1),
    ]
    assert (await _check(repo)).duplicate_of == 20


async def test_outside_window_ignored() -> None:
    fields = Fields()
    old = _recent(CFG.window_h + 1)
    repo = FakeRepo(
        [
            Stored(1, old, canonical_url=fields.canonical_url),
            Stored(2, old, content_hash=fields.content_hash),
            Stored(3, old, simhash=BASE_HASH),
        ]
    )
    assert not (await _check(repo, fields)).is_duplicate


async def test_news_not_compared_with_itself() -> None:
    fields = Fields()
    repo = FakeRepo(
        [
            Stored(
                ITEM_ID,
                _recent(),
                canonical_url=fields.canonical_url,
                content_hash=fields.content_hash,
                simhash=BASE_HASH,
            )
        ]
    )
    assert await _check(repo, fields) == DedupResult(False, None, ITEM_ID, None)


@pytest.mark.parametrize("value", [0, 2**63, 2**64 - 1])
async def test_signed_and_unsigned_simhash_compare(value: int) -> None:
    repo = FakeRepo([Stored(1, _recent(), simhash=value)])
    assert (await _check(repo, simhash=to_db(value))).duplicate_of == 1
    assert (await _check(repo, simhash=value)).duplicate_of == 1


def test_news_repository_fits_protocol() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.news import NewsRepository

    deduplicator = Deduplicator(NewsRepository(MagicMock()), CFG)
    assert isinstance(deduplicator, Deduplicator)
