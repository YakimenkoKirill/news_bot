from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AiRequest, NewsItem, Source
from app.db.repositories.ai_requests import AiRequestRepository
from app.domain.enums import NewsStatus, SourceType

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
KIND = "summary"
MODEL = "deepseek-flash"


async def _news(session: AsyncSession) -> int:
    source = Source(type=SourceType.RSS, url="https://r.example/rss", name="R", interval_min=10)
    session.add(source)
    await session.flush()
    item = NewsItem(
        source_id=source.id,
        external_id="1",
        url="https://r.example/1",
        title="t",
        raw_text="r",
        status=NewsStatus.NORMALIZED,
    )
    session.add(item)
    await session.flush()
    return item.id


def _request(
    news_id: int | None,
    cost: str,
    *,
    created_at: datetime = NOW,
    ok: bool = True,
    cached: int = 0,
) -> AiRequest:
    return AiRequest(
        news_item_id=news_id,
        kind=KIND,
        model=MODEL,
        prompt_tokens=120,
        cached_tokens=cached,
        completion_tokens=40,
        cost=Decimal(cost),
        ok=ok,
        created_at=created_at,
    )


async def test_add_assigns_id_and_keeps_fields(session: AsyncSession) -> None:
    news_id = await _news(session)
    repo = AiRequestRepository(session)

    saved = await repo.add(_request(news_id, "0.000420", cached=100))

    assert saved.id is not None
    stored = (await session.scalars(select(AiRequest))).one()
    assert stored.news_item_id == news_id
    assert (stored.kind, stored.model) == (KIND, MODEL)
    assert (stored.prompt_tokens, stored.cached_tokens, stored.completion_tokens) == (120, 100, 40)
    assert stored.cost == Decimal("0.000420")
    assert stored.ok is True


async def test_created_at_filled_by_database(session: AsyncSession) -> None:
    news_id = await _news(session)
    request = AiRequest(
        news_item_id=news_id,
        kind=KIND,
        model=MODEL,
        prompt_tokens=0,
        completion_tokens=0,
        cost=Decimal(0),
        ok=False,
    )
    await AiRequestRepository(session).add(request)
    await session.refresh(request)
    assert request.created_at.tzinfo is not None
    assert request.cached_tokens == 0


async def test_cost_since_window_counts_failed_requests(session: AsyncSession) -> None:
    news_id = await _news(session)
    repo = AiRequestRepository(session)
    assert await repo.cost_since(NOW - timedelta(days=1)) == Decimal("0")

    await repo.add(_request(news_id, "0.002", created_at=NOW - timedelta(hours=2)))
    await repo.add(_request(news_id, "0.003", created_at=NOW, ok=False))
    await repo.add(_request(news_id, "0.070", created_at=NOW - timedelta(days=2)))

    assert await repo.cost_since(NOW - timedelta(hours=3)) == Decimal("0.005")
    assert await repo.cost_since(NOW) == Decimal("0.003")
    assert await repo.cost_since(NOW + timedelta(seconds=1)) == Decimal("0")
    assert await repo.cost_since(NOW - timedelta(days=3)) == Decimal("0.075")


async def test_request_survives_news_deletion(session: AsyncSession) -> None:
    news_id = await _news(session)
    repo = AiRequestRepository(session)
    await repo.add(_request(news_id, "0.004", ok=False))

    await session.execute(delete(NewsItem).where(NewsItem.id == news_id))
    await session.flush()

    stored = (await session.scalars(select(AiRequest))).one()
    assert stored.news_item_id is None
    assert await repo.cost_since(NOW - timedelta(hours=1)) == Decimal("0.004")


async def test_request_without_news(session: AsyncSession) -> None:
    repo = AiRequestRepository(session)
    await repo.add(_request(None, "0.001"))
    assert await repo.cost_since(NOW - timedelta(hours=1)) == Decimal("0.001")
