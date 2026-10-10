from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aioresponses import aioresponses
from yarl import URL

from app.collectors.base import BaseCollector, PermanentCollectorError
from app.collectors.http import HttpClient
from app.collectors.web import WebCollector
from app.db.models import Source
from app.domain.enums import SourceType
from tests.config_helpers import collectors_config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "web"
LIST_URL = "https://site-a.example/news/"
SELECTORS = {
    "list_item": "article.news-card",
    "link": "a.news-card__link",
    "title": ".news-card__title",
}
CPU = "https://site-a.example/news/2026/10/cpu"
SPEECH = "https://site-a.example/news/news/2026/10/speech"
PARTNER = "https://partner.example.net/story/42"


class FakeRobots:
    def __init__(self, denied: set[str] | None = None) -> None:
        self.denied = denied or set()
        self.checked: list[str] = []

    async def allowed(self, url: str) -> bool:
        self.checked.append(url)
        return url not in self.denied


class KnownUrls:
    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known or set()

    async def __call__(self, url: str) -> bool:
        return url in self.known


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    client = HttpClient(collectors_config(per_domain_rps=1000, max_retries=0), sleep=_no_sleep)
    await client.start()
    yield client
    await client.close()


def _collector(
    http: HttpClient,
    robots: FakeRobots | None = None,
    known: set[str] | None = None,
    **config: Any,
) -> WebCollector:
    return WebCollector(http, robots or FakeRobots(), KnownUrls(known), collectors_config(**config))


def _source(selectors: dict[str, Any] | None = None) -> Source:
    return Source(
        id=3,
        type=SourceType.WEB,
        url=LIST_URL,
        name="Сайт А",
        interval_min=30,
        selectors=SELECTORS if selectors is None else selectors,
    )


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _mock_site(mock: aioresponses) -> None:
    mock.get(LIST_URL, status=200, body=_fixture("list_site_a.html"))
    mock.get(CPU, status=200, body=_fixture("article_full.html"))
    mock.get(SPEECH, status=200, body=_fixture("article_minimal.html"))
    mock.get(PARTNER, status=200, body=_fixture("article_bad_date.html"))


def _requested(mock: aioresponses, url: str) -> int:
    return len(mock.requests.get(("GET", URL(url)), []))


async def test_collects_new_articles(http: HttpClient, mocked_http: aioresponses) -> None:
    _mock_site(mocked_http)
    items = await _collector(http).fetch(_source())
    assert [item.external_id for item in items] == [CPU, SPEECH, PARTNER]
    cpu = items[0]
    assert cpu.url == CPU
    assert cpu.source_id == 3
    assert cpu.title == "Компания представила процессор нового поколения"
    assert cpu.image_url == "https://site-a.example/images/cpu-2026.jpg"
    assert cpu.published_at is not None
    assert "процессора" in cpu.text


async def test_known_links_not_fetched(http: HttpClient, mocked_http: aioresponses) -> None:
    _mock_site(mocked_http)
    items = await _collector(http, known={CPU, PARTNER}).fetch(_source())
    assert [item.external_id for item in items] == [SPEECH]
    assert _requested(mocked_http, CPU) == 0
    assert _requested(mocked_http, PARTNER) == 0


async def test_articles_limit(http: HttpClient, mocked_http: aioresponses) -> None:
    _mock_site(mocked_http)
    items = await _collector(http, max_articles_per_run=2).fetch(_source())
    assert [item.external_id for item in items] == [CPU, SPEECH]
    assert _requested(mocked_http, PARTNER) == 0


async def test_limit_counts_only_new_links(http: HttpClient, mocked_http: aioresponses) -> None:
    _mock_site(mocked_http)
    items = await _collector(http, known={CPU}, max_articles_per_run=1).fetch(_source())
    assert [item.external_id for item in items] == [SPEECH]


async def test_robots_denies_list_page(http: HttpClient, mocked_http: aioresponses) -> None:
    _mock_site(mocked_http)
    collector = _collector(http, robots=FakeRobots({LIST_URL}))
    with pytest.raises(PermanentCollectorError, match="robots"):
        await collector.fetch(_source())
    assert _requested(mocked_http, LIST_URL) == 0


async def test_robots_denies_single_article(http: HttpClient, mocked_http: aioresponses) -> None:
    _mock_site(mocked_http)
    items = await _collector(http, robots=FakeRobots({SPEECH})).fetch(_source())
    assert [item.external_id for item in items] == [CPU, PARTNER]
    assert _requested(mocked_http, SPEECH) == 0


async def test_one_article_error_does_not_break_others(
    http: HttpClient, mocked_http: aioresponses
) -> None:
    mocked_http.get(LIST_URL, status=200, body=_fixture("list_site_a.html"))
    mocked_http.get(CPU, status=500)
    mocked_http.get(SPEECH, status=404)
    mocked_http.get(PARTNER, status=200, body=_fixture("article_bad_date.html"))
    items = await _collector(http).fetch(_source())
    assert [item.external_id for item in items] == [PARTNER]


async def test_short_article_skipped(http: HttpClient, mocked_http: aioresponses) -> None:
    mocked_http.get(LIST_URL, status=200, body=_fixture("list_site_a.html"))
    mocked_http.get(CPU, status=200, body=_fixture("article_short.html"))
    mocked_http.get(SPEECH, status=200, body=_fixture("article_minimal.html"))
    mocked_http.get(PARTNER, status=200, body=_fixture("article_short.html"))
    items = await _collector(http).fetch(_source())
    assert [item.external_id for item in items] == [SPEECH]


@pytest.mark.parametrize("selectors", [{}, {"list_item": "div[["}, {"bad": "x"}])
async def test_missing_or_invalid_selectors(
    http: HttpClient, mocked_http: aioresponses, selectors: dict[str, Any]
) -> None:
    with pytest.raises(PermanentCollectorError, match="selectors"):
        await _collector(http).fetch(_source(selectors))


async def test_list_page_404_is_permanent(http: HttpClient, mocked_http: aioresponses) -> None:
    mocked_http.get(LIST_URL, status=404)
    with pytest.raises(PermanentCollectorError):
        await _collector(http).fetch(_source())


def test_web_collector_is_base_collector() -> None:
    assert issubclass(WebCollector, BaseCollector)
