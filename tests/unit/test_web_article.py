from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.collectors.web_article import parse_article, parse_datetime_utc
from tests.config_helpers import collectors_config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "web"
MIN_CHARS = collectors_config().min_article_chars


def _html(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_full_article() -> None:
    article = parse_article(
        _html("article_full.html"), "https://site-a.example/news/2026/10/cpu", MIN_CHARS
    )
    assert article is not None
    assert article.title == "Компания представила процессор нового поколения"
    assert article.image_url == "https://site-a.example/images/cpu-2026.jpg"
    assert article.published_at == datetime(2026, 10, 10, 6, 30, tzinfo=UTC)
    assert "на 30 процентов быстрее" in article.text
    assert "конкуренцию" in article.text
    assert "КОММЕНТАРИЙ" not in article.text
    assert "ТАБЛИЦА-12" not in article.text
    assert len(article.text) >= MIN_CHARS


def test_article_without_image_and_date() -> None:
    article = parse_article(_html("article_minimal.html"), "https://site-b.example/p/1", MIN_CHARS)
    assert article is not None
    assert article.title == "Город открыл новую станцию метро"
    assert article.image_url is None
    assert article.published_at is None
    assert "зелёной линии" in article.text


def test_too_short_text_returns_none() -> None:
    assert parse_article(_html("article_short.html"), "https://e.com/a", MIN_CHARS) is None


def test_min_chars_boundary() -> None:
    html = _html("article_minimal.html")
    article = parse_article(html, "https://site-b.example/p/1", MIN_CHARS)
    assert article is not None
    length = len(article.text)
    assert parse_article(html, "https://site-b.example/p/1", length) is not None
    assert parse_article(html, "https://site-b.example/p/1", length + 1) is None


def test_bad_date_does_not_break_parsing() -> None:
    article = parse_article(_html("article_bad_date.html"), "https://e.com/fx", MIN_CHARS)
    assert article is not None
    assert article.published_at is None
    assert article.image_url is None
    assert article.title == "Курс валют на сегодня"


def test_empty_page_returns_none() -> None:
    assert parse_article("", "https://e.com/a", MIN_CHARS) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-10T12:00:00Z", datetime(2026, 10, 10, 12, 0, tzinfo=UTC)),
        ("2026-10-10T15:00:00+03:00", datetime(2026, 10, 10, 12, 0, tzinfo=UTC)),
        ("2026-10-10T12:00:00", datetime(2026, 10, 10, 12, 0, tzinfo=UTC)),
        ("not a date", None),
    ],
)
def test_parse_datetime_utc(value: str, expected: datetime | None) -> None:
    assert parse_datetime_utc(value) == expected
