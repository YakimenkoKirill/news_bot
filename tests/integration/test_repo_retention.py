from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import NewsItem, Source
from app.db.repositories.news import NewsRepository
from app.domain.enums import NewsStatus, SourceType
from app.domain.status import TERMINAL_STATUSES
from app.pipeline.dedup import simhash_bands, to_db

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
BEFORE = NOW - timedelta(days=30)
OLD = BEFORE - timedelta(days=1)
RAW = "Сырой текст новости"
CLEAN = "Чистый текст новости"
SIMHASH = 0xABCD_1234_5678_9ABC
S = NewsStatus
IN_PROGRESS = (
    S.NEW,
    S.NORMALIZED,
    S.PROCESSED,
    S.PENDING_REVIEW,
    S.APPROVED,
    S.SCHEDULED,
    S.FAILED,
)


@dataclass(frozen=True)
class Raw:
    source_id: int
    external_id: str
    url: str = "https://r.example/news/1"
    title: str = "Заголовок"
    text: str = RAW
    image_url: str | None = None
    published_at: datetime | None = None
    raw_meta: Mapping[str, Any] = field(default_factory=dict)


async def _source(session: AsyncSession) -> int:
    existing = await session.scalar(select(Source.id).limit(1))
    if existing is not None:
        return int(existing)
    source = Source(type=SourceType.RSS, url="https://r.example/rss", name="R", interval_min=10)
    session.add(source)
    await session.flush()
    return source.id


async def _news(
    session: AsyncSession,
    external_id: str,
    status: NewsStatus,
    created_at: datetime = OLD,
    *,
    clean_text: str | None = CLEAN,
) -> int:
    source_id = await _source(session)
    item = await NewsRepository(session).add_if_new(Raw(source_id, external_id))
    assert item is not None
    bands = simhash_bands(SIMHASH)
    await session.execute(
        update(NewsItem)
        .where(NewsItem.id == item.id)
        .values(
            status=status,
            created_at=created_at,
            clean_text=clean_text,
            content_hash="hash",
            canonical_url="https://r.example/canonical",
            cluster_id=item.id,
            simhash=to_db(SIMHASH),
            raw_meta={"msg_id": "7"},
            **{f"simhash_b{index}": band for index, band in enumerate(bands)},
        )
    )
    return item.id


async def _row(session: AsyncSession, news_id: int) -> NewsItem:
    item = await NewsRepository(session).get(news_id)
    assert item is not None
    await session.refresh(item)
    return item


async def test_terminal_statuses_are_stripped(session: AsyncSession) -> None:
    ids = {
        status: await _news(session, status.value, status) for status in sorted(TERMINAL_STATUSES)
    }
    assert set(ids) == {S.PUBLISHED, S.FILTERED, S.DUPLICATE, S.REJECTED}

    stripped = await NewsRepository(session).strip_texts(BEFORE, 100)

    assert stripped == len(ids)
    for news_id in ids.values():
        item = await _row(session, news_id)
        assert item.raw_text == ""
        assert item.clean_text is None


async def test_news_in_progress_never_stripped(session: AsyncSession) -> None:
    ids = [await _news(session, status.value, status) for status in IN_PROGRESS]

    assert await NewsRepository(session).strip_texts(BEFORE, 100) == 0

    for news_id in ids:
        item = await _row(session, news_id)
        assert item.raw_text == RAW
        assert item.clean_text == CLEAN


async def test_fresh_news_kept(session: AsyncSession) -> None:
    fresh = await _news(session, "fresh", S.PUBLISHED, created_at=BEFORE)
    old = await _news(session, "old", S.PUBLISHED, created_at=BEFORE - timedelta(seconds=1))

    assert await NewsRepository(session).strip_texts(BEFORE, 100) == 1
    assert (await _row(session, fresh)).raw_text == RAW
    assert (await _row(session, old)).raw_text == ""


async def test_dedup_and_report_fields_kept(session: AsyncSession) -> None:
    news_id = await _news(session, "one", S.DUPLICATE)
    await NewsRepository(session).strip_texts(BEFORE, 100)

    item = await _row(session, news_id)
    assert item.title == "Заголовок"
    assert item.url == "https://r.example/news/1"
    assert item.canonical_url == "https://r.example/canonical"
    assert item.content_hash == "hash"
    assert item.simhash == to_db(SIMHASH)
    assert item.simhash_b0 == simhash_bands(SIMHASH)[0]
    assert item.cluster_id == news_id
    assert item.raw_meta == {"msg_id": "7"}
    assert item.status is S.DUPLICATE


async def test_already_stripped_not_counted_again(session: AsyncSession) -> None:
    await _news(session, "one", S.FILTERED)
    repo = NewsRepository(session)

    assert await repo.strip_texts(BEFORE, 100) == 1
    assert await repo.strip_texts(BEFORE, 100) == 0


async def test_news_without_clean_text_stripped_once(session: AsyncSession) -> None:
    news_id = await _news(session, "one", S.FILTERED, clean_text=None)
    repo = NewsRepository(session)

    assert await repo.strip_texts(BEFORE, 100) == 1
    assert (await _row(session, news_id)).raw_text == ""
    assert await repo.strip_texts(BEFORE, 100) == 0


async def test_batches(session: AsyncSession) -> None:
    for index in range(5):
        await _news(session, str(index), S.PUBLISHED)
    repo = NewsRepository(session)

    assert await repo.strip_texts(BEFORE, 2) == 2
    assert await repo.strip_texts(BEFORE, 2) == 2
    assert await repo.strip_texts(BEFORE, 2) == 1
    assert await repo.strip_texts(BEFORE, 2) == 0


async def test_oldest_stripped_first(session: AsyncSession) -> None:
    newer = await _news(session, "newer", S.PUBLISHED, created_at=OLD)
    older = await _news(session, "older", S.PUBLISHED, created_at=OLD - timedelta(days=10))

    assert await NewsRepository(session).strip_texts(BEFORE, 1) == 1
    assert (await _row(session, older)).raw_text == ""
    assert (await _row(session, newer)).raw_text == RAW


async def test_non_positive_limit_rejected(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="limit"):
        await NewsRepository(session).strip_texts(BEFORE, 0)


async def test_locked_rows_are_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Строку держит другая транзакция — очистка её пропускает и не ждёт."""
    async with session_scope(session_factory) as setup:
        held = await _news(setup, "held", S.PUBLISHED)
        free = await _news(setup, "free", S.PUBLISHED, created_at=OLD + timedelta(hours=1))

    async with session_factory() as holder, holder.begin():
        locked = await holder.scalars(select(NewsItem).where(NewsItem.id == held).with_for_update())
        assert locked.one().id == held

        async with session_scope(session_factory) as cleaner:
            assert await NewsRepository(cleaner).strip_texts(BEFORE, 100) == 1

    async with session_scope(session_factory) as check:
        assert (await _row(check, held)).raw_text == RAW
        assert (await _row(check, free)).raw_text == ""
