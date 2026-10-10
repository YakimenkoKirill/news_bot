from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses
from yarl import URL

from app.collectors.base import PermanentCollectorError, TemporaryCollectorError
from app.collectors.http import HttpClient, parse_retry_after
from app.config import CollectorsConfig

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CONFIG = CollectorsConfig(
    user_agent="TestBot/1.0 (+https://t.me/test)",
    http_timeout_s=20,
    per_domain_rps=1,
    max_retries=3,
)


class FakeTime:
    """Подменённые часы: sleep мгновенно сдвигает время."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
async def client(fake_time: FakeTime) -> AsyncIterator[HttpClient]:
    http = HttpClient(CONFIG, clock=fake_time.clock, sleep=fake_time.sleep, now=lambda: NOW)
    await http.start()
    yield http
    await http.close()


@pytest.fixture
def mocked(mocked_http: aioresponses) -> aioresponses:
    return mocked_http


def _calls(mock: aioresponses, url: str) -> list[Any]:
    return list(mock.requests.get(("GET", URL(url)), []))


async def test_returns_response_and_sends_user_agent(
    client: HttpClient, mocked: aioresponses
) -> None:
    url = "https://example.com/feed"
    mocked.get(url, status=200, body=b"<rss/>", headers={"ETag": '"abc"'})
    response = await client.get(url, headers={"If-None-Match": '"old"'})
    assert response.status == 200
    assert response.body == b"<rss/>"
    assert response.url == url
    assert response.headers["etag"] == '"abc"'
    [call] = _calls(mocked, url)
    sent = call.kwargs["headers"]
    assert sent["User-Agent"] == CONFIG.user_agent
    assert sent["If-None-Match"] == '"old"'


async def test_retries_on_503(
    client: HttpClient, mocked: aioresponses, fake_time: FakeTime
) -> None:
    url = "https://example.com/a"
    mocked.get(url, status=503)
    mocked.get(url, status=503)
    mocked.get(url, status=200, body=b"ok")
    response = await client.get(url)
    assert response.body == b"ok"
    assert len(_calls(mocked, url)) == 3
    assert len(fake_time.sleeps) >= 2


async def test_404_is_permanent_without_retry(client: HttpClient, mocked: aioresponses) -> None:
    url = "https://example.com/missing"
    mocked.get(url, status=404)
    with pytest.raises(PermanentCollectorError, match="404"):
        await client.get(url)
    assert len(_calls(mocked, url)) == 1


async def test_429_waits_retry_after_seconds(
    client: HttpClient, mocked: aioresponses, fake_time: FakeTime
) -> None:
    url = "https://example.com/limited"
    mocked.get(url, status=429, headers={"Retry-After": "7"})
    mocked.get(url, status=200, body=b"ok")
    assert (await client.get(url)).body == b"ok"
    assert 7 in fake_time.sleeps


async def test_429_retry_after_http_date(
    client: HttpClient, mocked: aioresponses, fake_time: FakeTime
) -> None:
    url = "https://example.com/limited"
    retry_at = format_datetime(NOW + timedelta(seconds=30), usegmt=True)
    mocked.get(url, status=429, headers={"Retry-After": retry_at})
    mocked.get(url, status=200, body=b"ok")
    assert (await client.get(url)).status == 200
    assert 30 in fake_time.sleeps


async def test_retries_exhausted_is_temporary(client: HttpClient, mocked: aioresponses) -> None:
    url = "https://example.com/down"
    for _ in range(CONFIG.max_retries + 1):
        mocked.get(url, status=502)
    with pytest.raises(TemporaryCollectorError, match="повторы исчерпаны"):
        await client.get(url)
    assert len(_calls(mocked, url)) == CONFIG.max_retries + 1


@pytest.mark.parametrize("error", [TimeoutError(), aiohttp.ClientConnectionError("refused")])
async def test_retries_network_errors(
    client: HttpClient, mocked: aioresponses, error: Exception
) -> None:
    url = "https://example.com/flaky"
    mocked.get(url, exception=error)
    mocked.get(url, status=200, body=b"ok")
    assert (await client.get(url)).body == b"ok"


async def test_304_returned_without_retry(client: HttpClient, mocked: aioresponses) -> None:
    url = "https://example.com/feed"
    mocked.get(url, status=304)
    response = await client.get(url)
    assert response.status == 304
    assert response.body == b""
    assert len(_calls(mocked, url)) == 1


async def test_redirect_keeps_final_url(client: HttpClient, mocked: aioresponses) -> None:
    start = "https://example.com/old"
    final = "https://example.com/new"
    mocked.get(start, status=301, headers={"Location": final})
    mocked.get(final, status=200, body=b"moved")
    response = await client.get(start)
    assert response.url == final
    assert response.body == b"moved"


async def test_per_domain_rate_limit(
    client: HttpClient, mocked: aioresponses, fake_time: FakeTime
) -> None:
    times: dict[str, list[float]] = {"a": [], "b": []}

    def record(key: str) -> Any:
        def callback(url: URL, **kwargs: Any) -> CallbackResult:
            times[key].append(fake_time.now)
            return CallbackResult(status=200, body=b"ok")

        return callback

    mocked.get("https://a.example/1", callback=record("a"))
    mocked.get("https://a.example/2", callback=record("a"))
    mocked.get("https://a.example/3", callback=record("a"))
    mocked.get("https://b.example/1", callback=record("b"))

    await client.get("https://a.example/1")
    await client.get("https://b.example/1")
    await client.get("https://a.example/2")
    await client.get("https://a.example/3")

    assert times["a"] == [0.0, 1.0, 2.0]
    assert times["b"] == [0.0]


async def test_get_requires_start(fake_time: FakeTime) -> None:
    http = HttpClient(CONFIG, clock=fake_time.clock, sleep=fake_time.sleep)
    with pytest.raises(RuntimeError, match="start"):
        await http.get("https://example.com")


def test_parse_retry_after() -> None:
    assert parse_retry_after("120", NOW) == 120
    assert parse_retry_after(None, NOW) is None
    assert parse_retry_after("", NOW) is None
    assert parse_retry_after("not a date", NOW) is None
    past = format_datetime(NOW - timedelta(minutes=1), usegmt=True)
    assert parse_retry_after(past, NOW) == 0
