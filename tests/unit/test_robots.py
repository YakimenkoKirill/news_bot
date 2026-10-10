import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from aioresponses import CallbackResult, aioresponses
from yarl import URL

from app.collectors.http import HttpClient
from app.collectors.robots import RobotsChecker
from tests.config_helpers import collectors_config

UA = "MyNewsBot/1.0 (+https://t.me/test)"
CONFIG = collectors_config(user_agent=UA, per_domain_rps=1000, max_retries=0)
TTL = timedelta(hours=CONFIG.robots_ttl_h)
ROBOTS = "https://example.com/robots.txt"
ROBOTS_BODY = b"""
User-agent: MyNewsBot
Disallow: /private/
Allow: /

User-agent: *
Disallow: /
"""


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def checker(clock: FakeClock) -> AsyncIterator[RobotsChecker]:
    http = HttpClient(CONFIG, clock=clock, sleep=_no_sleep)
    await http.start()
    yield RobotsChecker(http, UA, TTL, clock=clock)
    await http.close()


def _robots_calls(mock: aioresponses) -> int:
    return len(mock.requests.get(("GET", URL(ROBOTS)), []))


async def test_allow_and_disallow_for_our_agent(
    checker: RobotsChecker, mocked_http: aioresponses
) -> None:
    mocked_http.get(ROBOTS, status=200, body=ROBOTS_BODY)
    assert await checker.allowed("https://example.com/news/1")
    assert not await checker.allowed("https://example.com/private/secret")
    assert _robots_calls(mocked_http) == 1


async def test_other_agents_rules_do_not_apply(clock: FakeClock, mocked_http: aioresponses) -> None:
    http = HttpClient(CONFIG, clock=clock, sleep=_no_sleep)
    await http.start()
    try:
        mocked_http.get(ROBOTS, status=200, body=ROBOTS_BODY)
        other = RobotsChecker(http, "OtherBot/2.0", TTL, clock=clock)
        assert not await other.allowed("https://example.com/news/1")
    finally:
        await http.close()


async def test_404_allows_everything(checker: RobotsChecker, mocked_http: aioresponses) -> None:
    mocked_http.get(ROBOTS, status=404)
    assert await checker.allowed("https://example.com/private/x")


async def test_5xx_disallows_everything(checker: RobotsChecker, mocked_http: aioresponses) -> None:
    mocked_http.get(ROBOTS, status=503)
    assert not await checker.allowed("https://example.com/news/1")


async def test_unreachable_disallows_everything(
    checker: RobotsChecker, mocked_http: aioresponses
) -> None:
    mocked_http.get(ROBOTS, exception=TimeoutError())
    assert not await checker.allowed("https://example.com/news/1")


async def test_cache_within_ttl_and_refetch_after(
    checker: RobotsChecker, mocked_http: aioresponses, clock: FakeClock
) -> None:
    mocked_http.get(ROBOTS, status=200, body=ROBOTS_BODY)
    mocked_http.get(ROBOTS, status=200, body=b"User-agent: *\nDisallow: /\n")

    assert await checker.allowed("https://example.com/a")
    clock.now += TTL.total_seconds() - 1
    assert await checker.allowed("https://example.com/b")
    assert _robots_calls(mocked_http) == 1

    clock.now += 1
    assert not await checker.allowed("https://example.com/c")
    assert _robots_calls(mocked_http) == 2


async def test_cache_is_per_domain(checker: RobotsChecker, mocked_http: aioresponses) -> None:
    mocked_http.get(ROBOTS, status=200, body=ROBOTS_BODY)
    mocked_http.get("https://other.example/robots.txt", status=404)
    assert not await checker.allowed("https://example.com/private/1")
    assert await checker.allowed("https://other.example/private/1")


async def test_parallel_calls_make_single_request(
    checker: RobotsChecker, mocked_http: aioresponses
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(url: URL, **kwargs: Any) -> CallbackResult:
        started.set()
        await release.wait()
        return CallbackResult(status=200, body=ROBOTS_BODY)

    mocked_http.get(ROBOTS, callback=slow)
    tasks = [
        asyncio.create_task(checker.allowed(f"https://example.com/news/{i}")) for i in range(5)
    ]
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    assert results == [True] * 5
    assert _robots_calls(mocked_http) == 1
