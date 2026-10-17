from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NewsItem, Source
from app.db.repositories.news import NewsRepository
from app.domain.enums import SourceType
from app.pipeline.dedup import simhash_bands, to_db

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(hours=48)
BASE = 0xABCD_1234_5678_9ABC


@dataclass(frozen=True)
class Raw:
    source_id: int
    external_id: str
    url: str = "https://s.example/news/1"
    title: str = "Заголовок"
    text: str = "Текст новости"
    image_url: str | None = None
    published_at: datetime | None = None
    raw_meta: Mapping[str, Any] = field(default_factory=dict)


async def _source(session: AsyncSession) -> int:
    """Источник теста; один на сессию, чтобы новости попадали в одно окно."""
    existing = await session.scalar(select(Source.id).limit(1))
    if existing is not None:
        return int(existing)
    source = Source(type=SourceType.RSS, url="https://s.example/rss", name="S", interval_min=10)
    session.add(source)
    await session.flush()
    return source.id


async def _news(
    session: AsyncSession,
    external_id: str,
    simhash: int | None = None,
    *,
    created_at: datetime = NOW,
    cluster_id: int | None = None,
) -> int:
    repo = NewsRepository(session)
    source_id = await _source(session)
    item = await repo.add_if_new(Raw(source_id, external_id))
    assert item is not None
    values: dict[str, Any] = {"created_at": created_at, "cluster_id": cluster_id}
    if simhash is not None:
        bands = simhash_bands(simhash)
        values["simhash"] = to_db(simhash)
        values.update({f"simhash_b{index}": band for index, band in enumerate(bands)})
    await session.execute(update(NewsItem).where(NewsItem.id == item.id).values(**values))
    return item.id


def _flip(value: int, bits: Sequence[int]) -> int:
    for bit in bits:
        value ^= 1 << bit
    return value


async def test_candidate_found_when_one_band_matches(session: AsyncSession) -> None:
    """Расстояние 3, различия в одной части: остальные три совпадают точно."""
    near = _flip(BASE, (0, 1, 2))
    near_id = await _news(session, "near", near)
    far_id = await _news(session, "far", _flip(BASE, (0, 17, 33, 49)))

    rows = await NewsRepository(session).simhash_candidates(simhash_bands(BASE), WINDOW_START)

    assert [row[0] for row in rows] == [near_id]
    assert rows[0][1] == to_db(near)
    assert far_id not in [row[0] for row in rows]


async def test_distance_three_across_bands_is_found(session: AsyncSession) -> None:
    """Различия в трёх разных частях — четвёртая совпадает, кандидат находится."""
    near = _flip(BASE, (1, 20, 40))
    near_id = await _news(session, "near", near)

    rows = await NewsRepository(session).simhash_candidates(simhash_bands(BASE), WINDOW_START)
    assert [row[0] for row in rows] == [near_id]


async def test_distance_four_in_each_band_is_not_candidate(session: AsyncSession) -> None:
    await _news(session, "far", _flip(BASE, (1, 20, 40, 60)))
    rows = await NewsRepository(session).simhash_candidates(simhash_bands(BASE), WINDOW_START)
    assert rows == []


async def test_negative_simhash_bands_match(session: AsyncSession) -> None:
    """Знаковое хранение BIGINT не мешает: части считаются по тем же битам."""
    unsigned = 0xFFFF_FFFF_FFFF_FFF0
    assert to_db(unsigned) < 0
    near = _flip(unsigned, (0, 1))
    near_id = await _news(session, "negative", near)

    rows = await NewsRepository(session).simhash_candidates(simhash_bands(unsigned), WINDOW_START)
    assert [row[0] for row in rows] == [near_id]
    assert rows[0][1] < 0


async def test_window_and_missing_simhash_excluded(session: AsyncSession) -> None:
    fresh_id = await _news(session, "fresh", BASE, cluster_id=None)
    await _news(session, "old", BASE, created_at=NOW - timedelta(days=5))
    await _news(session, "no-hash")

    rows = await NewsRepository(session).simhash_candidates(simhash_bands(BASE), WINDOW_START)
    assert [row[0] for row in rows] == [fresh_id]


async def test_cluster_id_returned(session: AsyncSession) -> None:
    first_id = await _news(session, "first", BASE)
    second_id = await _news(session, "second", BASE, cluster_id=first_id)

    rows = await NewsRepository(session).simhash_candidates(simhash_bands(BASE), WINDOW_START)
    assert rows == [(first_id, to_db(BASE), None), (second_id, to_db(BASE), first_id)]


async def test_wrong_number_of_bands_rejected(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="частей simhash"):
        await NewsRepository(session).simhash_candidates((1, 2, 3), WINDOW_START)


async def test_candidates_query_uses_indexes(session: AsyncSession) -> None:
    """При выключенном enable_seqscan план не содержит Seq Scan по news_items."""
    await _news(session, "one", BASE)
    bands = simhash_bands(BASE)
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        row
        for row in (
            await session.execute(
                text(
                    "EXPLAIN SELECT id, simhash, cluster_id FROM news_items "
                    "WHERE created_at >= :since AND (simhash_b0 = :b0 OR simhash_b1 = :b1 "
                    "OR simhash_b2 = :b2 OR simhash_b3 = :b3) ORDER BY id"
                ).bindparams(
                    since=WINDOW_START,
                    b0=bands[0],
                    b1=bands[1],
                    b2=bands[2],
                    b3=bands[3],
                )
            )
        ).scalars()
    )
    assert "Seq Scan on news_items" not in plan, plan
    assert "ix_news_items_simhash_b" in plan, plan
