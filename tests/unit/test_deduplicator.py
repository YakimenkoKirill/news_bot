from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import DedupConfig
from app.pipeline.dedup import (
    DedupLevel,
    Deduplicator,
    DedupResult,
    compute_simhash,
    simhash_bands,
    to_db,
)
from scripts.eval_dedup import parse_records
from tests.config_helpers import base_section

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CFG = DedupConfig(**{**base_section("dedup"), "simhash_max_distance": 3, "window_h": 48})
ITEM_ID = 100
SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "dedup" / "sample.jsonl"
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
    band_calls: list[tuple[int, ...]] = field(default_factory=list)

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

    async def simhash_candidates(
        self, bands: Sequence[int], since: datetime
    ) -> list[tuple[int, int, int | None]]:
        """Отдаёт только новости с совпавшей частью SimHash — как индекс в БД."""
        self.calls.append("simhash")
        self.band_calls.append(tuple(bands))
        return [
            (i.id, to_db(i.simhash), i.cluster_id)
            for i in self._window(since, None)
            if i.simhash is not None
            and any(
                mine == theirs for mine, theirs in zip(bands, simhash_bands(i.simhash), strict=True)
            )
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


async def test_candidates_requested_by_bands() -> None:
    """В репозиторий уходят части SimHash проверяемой новости, а не всё окно."""
    repo = FakeRepo([Stored(1, _recent(), simhash=BASE_HASH)])
    await _check(repo)
    assert repo.band_calls == [simhash_bands(BASE_HASH)]
    assert repo.calls == ["url", "hash", "simhash"]


async def test_only_candidates_with_shared_band_are_compared() -> None:
    """Различия во всех четырёх частях: новость не попадает в кандидаты."""
    far = BASE_HASH ^ (1 << 1) ^ (1 << 20) ^ (1 << 40) ^ (1 << 60)
    repo = FakeRepo([Stored(5, _recent(), simhash=far)])
    assert not (await _check(repo)).is_duplicate


async def test_three_differences_in_different_bands_found() -> None:
    near = BASE_HASH ^ (1 << 2) ^ (1 << 20) ^ (1 << 40)
    repo = FakeRepo([Stored(6, _recent(), simhash=near)])
    assert (await _check(repo)).duplicate_of == 6


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


@dataclass
class FullScanRepo(FakeRepo):
    """Прежний поиск: отдаёт все новости окна, не глядя на части SimHash."""

    async def simhash_candidates(
        self, bands: Sequence[int], since: datetime
    ) -> list[tuple[int, int, int | None]]:
        self.calls.append("simhash")
        return [
            (i.id, to_db(i.simhash), i.cluster_id)
            for i in self._window(since, None)
            if i.simhash is not None
        ]


async def test_band_search_finds_same_duplicates_as_full_scan() -> None:
    """На размеченном наборе поиск по частям даёт тот же итог, что перебор окна."""
    records = parse_records(SAMPLE.read_text(encoding="utf-8").splitlines())
    chars = CFG.first_paragraph_chars
    hashes = [compute_simhash(record.title, record.text, chars) for record in records]

    by_bands: list[DedupResult] = []
    by_scan: list[DedupResult] = []
    for index, (record, value) in enumerate(zip(records, hashes, strict=True), start=1):
        stored = [
            Stored(position, _recent(), simhash=other, content_hash=f"h{position}")
            for position, other in enumerate(hashes[: index - 1], start=1)
        ]
        fields = Fields(canonical_url=f"https://example.com/{record.id}", content_hash=f"h{index}")
        by_bands.append(await Deduplicator(FakeRepo(stored), CFG).check(index, fields, value, NOW))
        by_scan.append(
            await Deduplicator(FullScanRepo(stored), CFG).check(index, fields, value, NOW)
        )

    assert by_bands == by_scan
    found = {records[index].cluster for index, result in enumerate(by_bands) if result.is_duplicate}
    assert found == {"rate"}, "при пороге 3 находится кластер «rate», как и перебором"
