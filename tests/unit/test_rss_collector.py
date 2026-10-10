from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aioresponses import aioresponses
from yarl import URL

from app.collectors.base import (
    BaseCollector,
    PermanentCollectorError,
    TemporaryCollectorError,
)
from app.collectors.http import HttpClient
from app.collectors.rss import RssCollector, entry_published_at
from app.db.models import Source
from app.domain.enums import SourceType
from tests.config_helpers import collectors_config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "rss"
FEED_URL = "https://news.example.com/rss"


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def collector() -> AsyncIterator[RssCollector]:
    http = HttpClient(collectors_config(per_domain_rps=1000, max_retries=0), sleep=_no_sleep)
    await http.start()
    yield RssCollector(http)
    await http.close()


def _source(fetch_state: dict[str, Any] | None = None) -> Source:
    return Source(
        id=1,
        type=SourceType.RSS,
        url=FEED_URL,
        name="Example",
        interval_min=10,
        fetch_state=fetch_state or {},
    )


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


async def test_rss2_feed(collector: RssCollector, mocked_http: aioresponses) -> None:
    mocked_http.get(
        FEED_URL,
        status=200,
        body=_fixture("rss2.xml"),
        headers={"ETag": '"v2"', "Last-Modified": "Sat, 10 Oct 2026 10:00:00 GMT"},
    )
    result = await collector.fetch_result(_source())
    items = result.items
    assert [i.external_id for i in items] == [
        "news-1",
        "https://news.example.com/articles/2",
        "news-3",
    ]
    first, second, third = items
    assert first.title == "Первая новость"
    assert first.url == "https://news.example.com/articles/1"
    assert first.published_at == datetime(2026, 10, 10, 6, 30, tzinfo=UTC)
    assert first.image_url == "https://cdn.news.example.com/1.jpg"
    assert "Краткое описание" in first.text
    assert first.raw_meta["tags"] == ["Технологии"]
    assert all(i.source_id == 1 for i in items)

    assert second.image_url == "https://cdn.news.example.com/2.png"
    assert second.published_at == datetime(2026, 10, 10, 8, 0, tzinfo=UTC)

    assert third.image_url == "https://cdn.news.example.com/3-thumb.jpg"
    assert third.text == "<p>Полный текст третьей новости.</p>"
    assert third.published_at == datetime(2026, 10, 10, 7, 0, tzinfo=UTC)

    assert result.fetch_state == {
        "etag": '"v2"',
        "last_modified": "Sat, 10 Oct 2026 10:00:00 GMT",
    }


async def test_atom_feed(collector: RssCollector, mocked_http: aioresponses) -> None:
    mocked_http.get(FEED_URL, status=200, body=_fixture("atom.xml"))
    items = await collector.fetch(_source())
    assert [i.external_id for i in items] == ["urn:uuid:entry-1", "urn:uuid:entry-2"]
    first, second = items
    assert first.url == "https://blog.example.org/posts/atom-1"
    assert first.published_at == datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
    assert first.image_url == "https://blog.example.org/img/atom-1.webp"
    assert first.text == "<p>Полный текст записи Atom.</p>"
    assert second.published_at is None
    assert second.image_url is None


async def test_conditional_headers_and_304(
    collector: RssCollector, mocked_http: aioresponses
) -> None:
    state = {"etag": '"v1"', "last_modified": "Fri, 09 Oct 2026 10:00:00 GMT"}
    mocked_http.get(FEED_URL, status=304)
    result = await collector.fetch_result(_source(state))
    assert result.items == []
    assert result.fetch_state == state
    [call] = mocked_http.requests[("GET", URL(FEED_URL))]
    sent = call.kwargs["headers"]
    assert sent["If-None-Match"] == '"v1"'
    assert sent["If-Modified-Since"] == "Fri, 09 Oct 2026 10:00:00 GMT"


async def test_no_conditional_headers_without_state(
    collector: RssCollector, mocked_http: aioresponses
) -> None:
    mocked_http.get(FEED_URL, status=200, body=_fixture("atom.xml"))
    result = await collector.fetch_result(_source())
    [call] = mocked_http.requests[("GET", URL(FEED_URL))]
    assert "If-None-Match" not in call.kwargs["headers"]
    assert "If-Modified-Since" not in call.kwargs["headers"]
    assert result.fetch_state == {}


async def test_broken_feed_with_entries_is_processed(
    collector: RssCollector, mocked_http: aioresponses
) -> None:
    mocked_http.get(FEED_URL, status=200, body=_fixture("broken_with_items.xml"))
    items = await collector.fetch(_source())
    assert [i.external_id for i in items] == ["broken-1"]


async def test_broken_feed_without_entries_is_temporary(
    collector: RssCollector, mocked_http: aioresponses
) -> None:
    mocked_http.get(FEED_URL, status=200, body=_fixture("broken_empty.xml"))
    with pytest.raises(TemporaryCollectorError, match="не разобрана"):
        await collector.fetch(_source())


async def test_http_errors_propagate(collector: RssCollector, mocked_http: aioresponses) -> None:
    mocked_http.get(FEED_URL, status=410)
    with pytest.raises(PermanentCollectorError):
        await collector.fetch(_source())


def test_collector_is_base_collector() -> None:
    assert issubclass(RssCollector, BaseCollector)


def test_naive_date_is_utc() -> None:
    entry = {"published": "2026-10-10T07:15:00"}
    assert entry_published_at(entry) == datetime(2026, 10, 10, 7, 15, tzinfo=UTC)
    assert entry_published_at({"updated": "garbage"}) is None
    assert entry_published_at({}) is None
