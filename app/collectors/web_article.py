"""Разбор страницы статьи: текст через trafilatura, метаданные из разметки."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import structlog
import trafilatura
from bs4 import BeautifulSoup, Tag

from app.collectors.web_list import ALLOWED_SCHEMES, HTML_PARSER

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ArticleData:
    """Извлечённые данные статьи."""

    title: str
    text: str
    image_url: str | None
    published_at: datetime | None


def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    if isinstance(tag, Tag):
        content = tag.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def _title(soup: BeautifulSoup) -> str:
    og_title = _meta(soup, "og:title")
    if og_title:
        return og_title
    if soup.title is not None:
        return " ".join(soup.title.get_text(" ").split())
    return ""


def _image(soup: BeautifulSoup, url: str) -> str | None:
    raw = _meta(soup, "og:image")
    if not raw:
        return None
    absolute = urljoin(url, raw)
    parts = urlsplit(absolute)
    return absolute if parts.scheme.lower() in ALLOWED_SCHEMES and parts.netloc else None


def parse_datetime_utc(value: str) -> datetime | None:
    """Разбирает ISO 8601 и приводит к UTC; время без пояса считается UTC.

    Returns:
        Время в UTC или None, если строка некорректна.
    """
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        log.warning("web_article.bad_date", value=value)
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _published_at(soup: BeautifulSoup) -> datetime | None:
    candidates: list[str] = []
    meta_time = _meta(soup, "article:published_time")
    if meta_time:
        candidates.append(meta_time)
    for time_tag in soup.find_all("time"):
        value = time_tag.get("datetime") if isinstance(time_tag, Tag) else None
        if isinstance(value, str) and value.strip():
            candidates.append(value)
    for candidate in candidates:
        parsed = parse_datetime_utc(candidate)
        if parsed is not None:
            return parsed
    return None


def parse_article(html: str | bytes, url: str, min_chars: int) -> ArticleData | None:
    """Извлекает заголовок, чистый текст, картинку и дату статьи.

    Args:
        html: HTML страницы статьи.
        url: Адрес статьи (для абсолютного адреса картинки).
        min_chars: Минимальная длина текста (collectors.min_article_chars).

    Returns:
        Данные статьи или None, если текста нет или он короче min_chars.
    """
    markup = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    text = trafilatura.extract(markup, url=url, include_comments=False, include_tables=False)
    if not text or len(text.strip()) < min_chars:
        log.info("web_article.too_short", url=url, chars=len(text or ""))
        return None
    soup = BeautifulSoup(markup, HTML_PARSER)
    return ArticleData(
        title=_title(soup),
        text=text.strip(),
        image_url=_image(soup, url),
        published_at=_published_at(soup),
    )
