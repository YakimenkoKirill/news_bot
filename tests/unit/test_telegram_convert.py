from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityTextUrl,
    MessageEntityUrl,
    PeerChannel,
)

from app.collectors.base import PermanentCollectorError
from app.collectors.telegram.convert import (
    make_title,
    message_to_raw_item,
    utf16_slice,
)
from app.db.models import Source
from app.domain.enums import SourceType

TITLE_MAX = 120
CHANNEL_ID = 1234567890
DATE = datetime(2026, 10, 10, 9, 15, tzinfo=UTC)


def _source() -> Source:
    return Source(
        id=9, type=SourceType.TG, url="https://t.me/technews", name="Tech", interval_min=1
    )


def _message(text: str = "", **fields: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": 501,
        "message": text,
        "entities": [],
        "photo": None,
        "date": DATE,
        "peer_id": PeerChannel(channel_id=CHANNEL_ID),
        "grouped_id": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def test_text_with_links() -> None:
    text = "Новый релиз библиотеки\nПодробности на сайте и в блоге example.com/post"
    site_start = text.index("сайте")
    blog_start = text.index("example.com/post")
    message = _message(
        text,
        entities=[
            MessageEntityBold(offset=0, length=5),
            MessageEntityTextUrl(offset=site_start, length=5, url="https://site.example/release"),
            MessageEntityUrl(offset=blog_start, length=len("example.com/post")),
        ],
    )
    item = message_to_raw_item(message, _source(), "technews", TITLE_MAX)
    assert item is not None
    assert item.external_id == f"{CHANNEL_ID}:501"
    assert item.url == "https://t.me/technews/501"
    assert item.title == "Новый релиз библиотеки"
    assert item.text == text
    assert item.published_at == DATE
    assert item.source_id == 9
    assert item.raw_meta == {
        "msg_id": 501,
        "channel_id": CHANNEL_ID,
        "has_photo": False,
        "links": ["https://site.example/release", "https://example.com/post"],
    }


def test_emoji_before_link_uses_utf16_offsets() -> None:
    text = "🚀🔥 Читайте https://news.example/a и 👍 https://b.example"
    first = "https://news.example/a"
    second = "https://b.example"
    utf16_offset = len(text[: text.index(first)].encode("utf-16-le")) // 2
    second_offset = len(text[: text.index(second)].encode("utf-16-le")) // 2
    assert utf16_offset == text.index(first) + 2
    message = _message(
        text,
        entities=[
            MessageEntityUrl(offset=utf16_offset, length=len(first)),
            MessageEntityUrl(offset=second_offset, length=len(second)),
        ],
    )
    item = message_to_raw_item(message, _source(), "technews", TITLE_MAX)
    assert item is not None
    assert item.raw_meta["links"] == [first, second]


def test_photo_only_message() -> None:
    message = _message("", photo=object(), grouped_id=777)
    item = message_to_raw_item(message, _source(), "@technews", TITLE_MAX)
    assert item is not None
    assert item.text == ""
    assert item.title == ""
    assert item.raw_meta["has_photo"] is True
    assert item.raw_meta["grouped_id"] == 777
    assert item.url == "https://t.me/technews/501"


@pytest.mark.parametrize("text", ["", "   \n  "])
def test_empty_message_returns_none(text: str) -> None:
    assert message_to_raw_item(_message(text), _source(), "technews", TITLE_MAX) is None


@pytest.mark.parametrize("username", [None, ""])
def test_channel_without_username(username: str | None) -> None:
    with pytest.raises(PermanentCollectorError, match="username"):
        message_to_raw_item(_message("текст"), _source(), username, TITLE_MAX)


def test_date_converted_to_utc() -> None:
    moscow = timezone(timedelta(hours=3))
    message = _message("текст", date=datetime(2026, 10, 10, 12, 15, tzinfo=moscow))
    item = message_to_raw_item(message, _source(), "technews", TITLE_MAX)
    assert item is not None
    assert item.published_at == DATE


def test_title_truncated() -> None:
    long_line = "Очень длинный заголовок " * 10
    message = _message(long_line + "\nвторая строка")
    item = message_to_raw_item(message, _source(), "technews", 30)
    assert item is not None
    assert len(item.title) <= 30
    assert item.title == long_line[:30].rstrip()


def test_make_title_skips_blank_lines() -> None:
    assert make_title("\n\n  Заголовок  \nтекст", TITLE_MAX) == "Заголовок"
    assert make_title("", TITLE_MAX) == ""


def test_utf16_slice() -> None:
    text = "😀ab"
    assert utf16_slice(text, 0, 2) == "😀"
    assert utf16_slice(text, 2, 2) == "ab"
