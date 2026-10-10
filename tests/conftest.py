from collections.abc import Iterator
from typing import Any

import pytest
from aiohttp import ClientResponse
from aioresponses import aioresponses, core


class _NullStreamWriter:
    """Заглушка писателя запроса: тело уже «отправлено», размер 0."""

    output_size = 0


class _CompatClientResponse(ClientResponse):
    """ClientResponse для aioresponses на aiohttp >= 3.14.

    aiohttp 3.14 требует аргумент stream_writer, который aioresponses 0.7.x не передаёт.
    """

    def __init__(self, method: str, url: Any, **kwargs: Any) -> None:
        kwargs.setdefault("stream_writer", _NullStreamWriter())
        super().__init__(method, url, **kwargs)


@pytest.fixture
def mocked_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[aioresponses]:
    """Подменяет сетевые запросы aiohttp; совместим с aiohttp 3.14."""
    monkeypatch.setattr(core, "ClientResponse", _CompatClientResponse)
    with aioresponses() as mock:
        yield mock
