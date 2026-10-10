from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.collectors.base import (
    BaseCollector,
    CollectorError,
    PermanentCollectorError,
    RawItem,
    TemporaryCollectorError,
)
from app.db.models import Source
from app.db.repositories.news import RawNews
from app.domain.enums import SourceType


def _raw(**overrides: Any) -> RawItem:
    fields: dict[str, Any] = {
        "source_id": 1,
        "external_id": "guid-1",
        "url": "https://example.com/news/1",
        "title": "Заголовок",
        "text": "Текст",
    }
    fields.update(overrides)
    return RawItem(**fields)


def test_valid_item_with_defaults() -> None:
    item = _raw()
    assert item.image_url is None
    assert item.published_at is None
    assert item.raw_meta == {}


def test_title_and_text_are_stripped() -> None:
    item = _raw(title="  Заголовок \n", text="\t Текст  ", external_id=" id ")
    assert item.title == "Заголовок"
    assert item.text == "Текст"
    assert item.external_id == "id"


@pytest.mark.parametrize("external_id", ["", "   "])
def test_empty_external_id_rejected(external_id: str) -> None:
    with pytest.raises(ValidationError, match="external_id"):
        _raw(external_id=external_id)


@pytest.mark.parametrize("source_id", [0, -1])
def test_source_id_must_be_positive(source_id: int) -> None:
    with pytest.raises(ValidationError, match="source_id"):
        _raw(source_id=source_id)


@pytest.mark.parametrize(
    "url", ["ftp://example.com/a", "javascript:alert(1)", "example.com/a", "https://", ""]
)
def test_non_http_url_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match="url"):
        _raw(url=url)


def test_http_and_https_urls_accepted() -> None:
    assert _raw(url="http://example.com").url == "http://example.com"
    assert _raw(url="HTTPS://example.com/x").url == "HTTPS://example.com/x"


def test_image_url_validated() -> None:
    assert (
        _raw(image_url="https://cdn.example.com/a.jpg").image_url == "https://cdn.example.com/a.jpg"
    )
    assert _raw(image_url="").image_url is None
    with pytest.raises(ValidationError, match="image_url"):
        _raw(image_url="data:image/png;base64,AAAA")


def test_naive_published_at_rejected() -> None:
    with pytest.raises(ValidationError, match="published_at"):
        _raw(published_at=datetime(2026, 10, 10, 12, 0))


def test_aware_published_at_accepted() -> None:
    moscow = timezone(timedelta(hours=3))
    item = _raw(published_at=datetime(2026, 10, 10, 12, 0, tzinfo=moscow))
    assert item.published_at == datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        _raw(unexpected="x")


def test_item_is_frozen() -> None:
    item = _raw()
    with pytest.raises(ValidationError):
        item.title = "другой"  # type: ignore[misc]


def test_raw_item_matches_repository_protocol() -> None:
    raw: RawNews = _raw(raw_meta={"msg_id": 5})
    assert raw.raw_meta["msg_id"] == 5


def test_error_hierarchy() -> None:
    assert issubclass(TemporaryCollectorError, CollectorError)
    assert issubclass(PermanentCollectorError, CollectorError)
    assert not issubclass(TemporaryCollectorError, PermanentCollectorError)
    assert TemporaryCollectorError("timeout").retryable is True
    assert PermanentCollectorError("404").retryable is False


def test_base_collector_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseCollector()  # type: ignore[abstract]


async def test_subclass_can_fetch() -> None:
    class StaticCollector(BaseCollector):
        async def fetch(self, source: Source) -> list[RawItem]:
            return [_raw(source_id=source.id)]

    source = Source(
        id=7, type=SourceType.RSS, url="https://example.com/rss", name="E", interval_min=5
    )
    items = await StaticCollector().fetch(source)
    assert [item.source_id for item in items] == [7]
