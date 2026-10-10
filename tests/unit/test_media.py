from collections.abc import AsyncIterator

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from app.collectors.http import HttpClient
from app.config import PublisherConfig
from app.publisher.media import ImageCheck, check_image
from tests.config_helpers import base_section, collectors_config

CFG = PublisherConfig(**base_section("publisher"))
IMG = "https://cdn.example/photo.jpg"


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    client = HttpClient(collectors_config(per_domain_rps=1000, max_retries=0), sleep=_no_sleep)
    await client.start()
    yield client
    await client.close()


def _headers(content_type: str = "image/jpeg", length: int | None = 1000) -> dict[str, str]:
    headers = {"Content-Type": content_type}
    if length is not None:
        headers["Content-Length"] = str(length)
    return headers


def _partial(content_type: str, total: int) -> dict[str, str]:
    return {"Content-Type": content_type, "Content-Range": f"bytes 0-0/{total}"}


async def _check(http: HttpClient, url: str | None = IMG) -> ImageCheck:
    return await check_image(url, http, CFG)


@pytest.mark.parametrize("content_type", ["image/jpeg", "image/webp", "image/png; charset=binary"])
async def test_allowed_types_pass(
    http: HttpClient, mocked_http: aioresponses, content_type: str
) -> None:
    mocked_http.head(IMG, status=200, headers=_headers(content_type))
    assert await _check(http) == ImageCheck(ok=True)
    assert ("GET", URL(IMG)) not in mocked_http.requests


async def test_svg_rejected(http: HttpClient, mocked_http: aioresponses) -> None:
    mocked_http.head(IMG, status=200, headers=_headers("image/svg+xml"))
    assert await _check(http) == ImageCheck(ok=False, reason="bad_type:image/svg+xml")


async def test_too_large(http: HttpClient, mocked_http: aioresponses) -> None:
    size = CFG.image_max_bytes + 1
    mocked_http.head(IMG, status=200, headers=_headers(length=size))
    assert await _check(http) == ImageCheck(ok=False, reason=f"too_large:{size}")


async def test_size_at_limit_ok(http: HttpClient, mocked_http: aioresponses) -> None:
    mocked_http.head(IMG, status=200, headers=_headers(length=CFG.image_max_bytes))
    assert (await _check(http)).ok


async def test_404(http: HttpClient, mocked_http: aioresponses) -> None:
    mocked_http.head(IMG, status=404)
    assert await _check(http) == ImageCheck(ok=False, reason="http_404")


async def test_405_falls_back_to_range_get(http: HttpClient, mocked_http: aioresponses) -> None:
    mocked_http.head(IMG, status=405)
    mocked_http.get(IMG, status=206, headers=_partial("image/jpeg", 2048))
    assert await _check(http) == ImageCheck(ok=True)
    [call] = mocked_http.requests[("GET", URL(IMG))]
    assert call.kwargs["headers"]["Range"] == "bytes=0-0"


async def test_head_without_headers_falls_back_to_get(
    http: HttpClient, mocked_http: aioresponses
) -> None:
    too_big = CFG.image_max_bytes * 2
    mocked_http.head(IMG, status=200, headers={"Content-Type": ""})
    mocked_http.get(IMG, status=206, headers=_partial("image/png", too_big))
    assert await _check(http) == ImageCheck(ok=False, reason=f"too_large:{too_big}")


async def test_missing_content_length_ok_with_warning(
    http: HttpClient, mocked_http: aioresponses
) -> None:
    mocked_http.head(IMG, status=200, headers=_headers(length=None))
    assert (await _check(http)).ok


async def test_redirect_to_other_domain_allowed(
    http: HttpClient, mocked_http: aioresponses
) -> None:
    final = "https://other-cdn.example/real.jpg"
    mocked_http.head(IMG, status=302, headers={"Location": final})
    # aioresponses после редиректа всегда переходит на GET (aiohttp сохранил бы HEAD).
    mocked_http.get(final, status=200, headers=_headers(length=5))
    assert (await _check(http)).ok


async def test_timeout(http: HttpClient, mocked_http: aioresponses) -> None:
    mocked_http.head(IMG, exception=TimeoutError())
    assert await _check(http) == ImageCheck(ok=False, reason="timeout")


async def test_connection_error_unavailable(http: HttpClient, mocked_http: aioresponses) -> None:
    mocked_http.head(IMG, exception=aiohttp.ClientConnectionError("refused"))
    assert await _check(http) == ImageCheck(ok=False, reason="unavailable")


@pytest.mark.parametrize("url", [None, ""])
async def test_no_image(http: HttpClient, url: str | None) -> None:
    assert await _check(http, url) == ImageCheck(ok=False, reason="no_image")
