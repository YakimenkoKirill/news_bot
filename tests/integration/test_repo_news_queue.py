from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import session_scope
from app.db.models import NewsItem, ProcessedPost, Source
from app.db.repositories.news import NewsRepository, Priority
from app.domain.enums import NewsStatus, SourceType

pytestmark = pytest.mark.integration

S = NewsStatus
BASE = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
QUEUE = [S.APPROVED]
DECAY = 0.02
PRIORITY = Priority(DECAY)


class Seeder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._source: Source | None = None
        self._counter = 0

    async def news(
        self,
        score: float | None,
        *,
        status: NewsStatus = S.APPROVED,
        published_at: datetime | None = BASE,
        created_at: datetime | None = None,
    ) -> int:
        if self._source is None:
            self._source = Source(
                type=SourceType.RSS, url="https://q.example/rss", name="Q", interval_min=10
            )
            self._session.add(self._source)
            await self._session.flush()
        self._counter += 1
        item = NewsItem(
            source_id=self._source.id,
            external_id=str(self._counter),
            url=f"https://q.example/{self._counter}",
            title=f"t{self._counter}",
            raw_text="r",
            status=status,
            published_at=published_at,
        )
        if created_at is not None:
            item.created_at = created_at
        self._session.add(item)
        await self._session.flush()
        if score is not None:
            self._session.add(
                ProcessedPost(
                    news_item_id=item.id,
                    ai_title="Заголовок",
                    ai_summary="Пересказ",
                    hashtags=[],
                    category=None,
                    relevance_score=score,
                    prompt_version="v1",
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost=Decimal("0.000001"),
                )
            )
            await self._session.flush()
        return item.id


async def _order(
    repo: NewsRepository, priority: Priority, statuses: list[NewsStatus] = QUEUE
) -> list[int]:
    rows = await repo.list_prepared(statuses, priority, limit=100, offset=0)
    return [news.id for news, _ in rows]


async def test_order_by_score_without_decay(session: AsyncSession) -> None:
    seed = Seeder(session)
    low = await seed.news(0.3)
    high = await seed.news(0.9)
    mid = await seed.news(0.6, published_at=BASE - timedelta(days=3))
    assert await _order(NewsRepository(session), Priority(0)) == [high, mid, low]


async def test_fresh_news_overtakes_older_with_decay(session: AsyncSession) -> None:
    seed = Seeder(session)
    old = await seed.news(0.8, published_at=BASE - timedelta(hours=12))
    fresh = await seed.news(0.6, published_at=BASE)
    repo = NewsRepository(session)
    assert await _order(repo, PRIORITY) == [fresh, old]
    assert await _order(repo, Priority(0)) == [old, fresh]


async def test_null_published_at_uses_created_at(session: AsyncSession) -> None:
    seed = Seeder(session)
    old = await seed.news(0.8, published_at=BASE - timedelta(hours=12))
    no_date = await seed.news(0.6, published_at=None, created_at=BASE)
    stale_no_date = await seed.news(0.7, published_at=None, created_at=BASE - timedelta(days=2))
    assert await _order(NewsRepository(session), PRIORITY) == [no_date, old, stale_no_date]


async def test_equal_priority_smaller_id_first(session: AsyncSession) -> None:
    seed = Seeder(session)
    first = await seed.news(0.5)
    second = await seed.news(0.5)
    third = await seed.news(0.5)
    assert await _order(NewsRepository(session), PRIORITY) == [first, second, third]


async def test_pagination_matches_claim_next(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        seed = Seeder(session)
        for hours, score in [(0, 0.4), (5, 0.9), (1, 0.5), (30, 0.95), (2, 0.7)]:
            await seed.news(score, published_at=BASE - timedelta(hours=hours))

    async with session_factory() as session:
        repo = NewsRepository(session)
        full = await _order(repo, PRIORITY)
        pages = [
            [n.id for n, _ in await repo.list_prepared(QUEUE, PRIORITY, limit=2, offset=offset)]
            for offset in (0, 2, 4)
        ]
        assert [news_id for page in pages for news_id in page] == full
        assert [len(page) for page in pages] == [2, 2, 1]
        assert await repo.list_prepared(QUEUE, PRIORITY, limit=2, offset=10) == []

    claimed: list[int] = []
    async with session_factory() as session, session.begin():
        repo = NewsRepository(session)
        while (item := await repo.claim_next(QUEUE, PRIORITY)) is not None:
            claimed.append(item.id)
            await repo.set_status(item.id, S.APPROVED, S.PUBLISHED)
    assert claimed == full


async def test_multiple_statuses_and_posts_required(session: AsyncSession) -> None:
    seed = Seeder(session)
    approved = await seed.news(0.5, status=S.APPROVED)
    scheduled = await seed.news(0.9, status=S.SCHEDULED)
    await seed.news(0.99, status=S.PENDING_REVIEW)
    await seed.news(None, status=S.APPROVED)
    repo = NewsRepository(session)

    assert await _order(repo, Priority(0), [S.APPROVED, S.SCHEDULED]) == [scheduled, approved]
    assert await repo.count_prepared([S.APPROVED, S.SCHEDULED]) == 2
    assert await repo.count_prepared([S.APPROVED]) == 1
    rows = await repo.list_prepared([S.APPROVED], Priority(0), limit=10, offset=0)
    news, post = rows[0]
    assert (news.id, post.news_item_id, post.relevance_score) == (approved, approved, 0.5)


async def test_claim_one_by_id(session: AsyncSession) -> None:
    seed = Seeder(session)
    approved = await seed.news(0.5)
    rejected = await seed.news(0.5, status=S.REJECTED)
    repo = NewsRepository(session)

    item = await repo.claim_one(approved, QUEUE)
    assert item is not None and item.id == approved
    assert await repo.claim_one(rejected, QUEUE) is None
    assert await repo.claim_one(10_000, QUEUE) is None


async def test_claims_skip_locked_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        seed = Seeder(session)
        top = await seed.news(0.9)
        second = await seed.news(0.5)

    async with (
        session_factory() as first,
        session_factory() as other,
        first.begin(),
        other.begin(),
    ):
        held = await NewsRepository(first).claim_one(top, QUEUE)
        assert held is not None and held.id == top

        repo = NewsRepository(other)
        assert await repo.claim_one(top, QUEUE) is None
        next_item = await repo.claim_next(QUEUE, Priority(0))
        assert next_item is not None and next_item.id == second
        assert await repo.claim_next(QUEUE, Priority(0)) is not None


@pytest.mark.parametrize(
    ("statuses", "priority", "limit", "offset"),
    [
        ([], Priority(0.0), 10, 0),
        (QUEUE, Priority(-0.1), 10, 0),
        (QUEUE, Priority(DECAY, cluster_bonus=-1), 10, 0),
        (QUEUE, Priority(DECAY, cluster_bonus_max=-1), 10, 0),
        (QUEUE, Priority(0.0), 0, 0),
        (QUEUE, Priority(0.0), -1, 0),
        (QUEUE, Priority(0.0), 10, -1),
    ],
)
async def test_list_prepared_invalid_arguments(
    session: AsyncSession, statuses: list[NewsStatus], priority: Priority, limit: int, offset: int
) -> None:
    with pytest.raises(ValueError):
        await NewsRepository(session).list_prepared(statuses, priority, limit, offset)


async def test_other_methods_invalid_arguments(session: AsyncSession) -> None:
    repo = NewsRepository(session)
    with pytest.raises(ValueError):
        await repo.count_prepared([])
    with pytest.raises(ValueError):
        await repo.claim_next([], Priority(0))
    with pytest.raises(ValueError):
        await repo.claim_next(QUEUE, Priority(-1))
    with pytest.raises(ValueError):
        await repo.claim_one(1, [])
