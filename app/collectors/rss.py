"""Сборщик RSS 2.0 и Atom с условными запросами (ETag, Last-Modified)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Any

import feedparser
import structlog
from pydantic import ValidationError

from app.collectors.base import (
    BaseCollector,
    FetchResult,
    RawItem,
    TemporaryCollectorError,
)
from app.collectors.http import HttpClient
from app.db.models import Source

log = structlog.get_logger(__name__)

ETAG_KEY = "etag"
LAST_MODIFIED_KEY = "last_modified"
IMAGE_MIME_PREFIX = "image/"
IMAGE_MEDIUM = "image"


def _to_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_raw_date(text: str) -> datetime | None:
    try:
        return _to_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        pass
    try:
        return _to_utc(datetime.fromisoformat(text))
    except ValueError:
        log.warning("rss.bad_date", value=text)
        return None


def _field(entry: Any, key: str) -> Any:
    """Читает поле записи напрямую: FeedParserDict.get подменяет updated на published
    с DeprecationWarning."""
    return dict.get(entry, key)


def entry_published_at(entry: Any) -> datetime | None:
    """Время публикации записи в UTC; дата без часового пояса считается UTC."""
    for key in ("published", "updated"):
        parsed: time.struct_time | None = _field(entry, f"{key}_parsed")
        if parsed is not None:
            return datetime(*parsed[:6], tzinfo=UTC)
    for key in ("published", "updated"):
        raw = _field(entry, key)
        if raw:
            return _parse_raw_date(str(raw))
    return None


def entry_image_url(entry: Any) -> str | None:
    """Картинка записи: media:content, затем media:thumbnail, затем enclosure image/*."""
    for media in entry.get("media_content") or []:
        mime = str(media.get("type") or "")
        medium = str(media.get("medium") or "")
        is_image = mime.startswith(IMAGE_MIME_PREFIX) or medium == IMAGE_MEDIUM
        if media.get("url") and (is_image or not (mime or medium)):
            return str(media["url"])
    for thumb in entry.get("media_thumbnail") or []:
        if thumb.get("url"):
            return str(thumb["url"])
    for enclosure in entry.get("enclosures") or []:
        href = enclosure.get("href")
        if href and str(enclosure.get("type") or "").startswith(IMAGE_MIME_PREFIX):
            return str(href)
    return None


def _entry_text(entry: Any) -> str:
    contents = entry.get("content") or []
    for content in contents:
        if content.get("value"):
            return str(content["value"])
    return str(entry.get("summary") or "")


def _entry_meta(entry: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if entry.get("author"):
        meta["author"] = str(entry["author"])
    tags = [str(tag["term"]) for tag in entry.get("tags") or [] if tag.get("term")]
    if tags:
        meta["tags"] = tags
    return meta


class RssCollector(BaseCollector):
    """Сборщик RSS/Atom-лент через общий HttpClient."""

    def __init__(self, http_client: HttpClient) -> None:
        self._http = http_client

    async def fetch(self, source: Source) -> list[RawItem]:
        """Возвращает новости ленты (без нового fetch_state)."""
        return (await self.fetch_result(source)).items

    async def fetch_result(self, source: Source) -> FetchResult:
        """Условно загружает ленту и разбирает записи.

        Args:
            source: Источник; из fetch_state берутся etag и last_modified.

        Returns:
            Новости и новое состояние; при 304 — пустой список и прежнее состояние.

        Raises:
            TemporaryCollectorError: Лента битая и в ней нет записей, или сеть недоступна.
            PermanentCollectorError: Лента отвечает 4xx.
        """
        state = dict(source.fetch_state or {})
        headers: dict[str, str] = {}
        if state.get(ETAG_KEY):
            headers["If-None-Match"] = str(state[ETAG_KEY])
        if state.get(LAST_MODIFIED_KEY):
            headers["If-Modified-Since"] = str(state[LAST_MODIFIED_KEY])

        response = await self._http.get(source.url, headers=headers)
        if response.status == HTTPStatus.NOT_MODIFIED:
            return FetchResult(items=[], fetch_state=state)

        feed = await asyncio.to_thread(feedparser.parse, response.body)
        if feed.bozo and not feed.entries:
            raise TemporaryCollectorError(
                f"лента {source.url} не разобрана: {feed.get('bozo_exception')!r}"
            )
        if feed.bozo:
            log.warning("rss.bozo_feed", url=source.url, error=repr(feed.get("bozo_exception")))

        new_state: dict[str, Any] = {}
        if response.headers.get("ETag"):
            new_state[ETAG_KEY] = response.headers["ETag"]
        if response.headers.get("Last-Modified"):
            new_state[LAST_MODIFIED_KEY] = response.headers["Last-Modified"]
        items = [item for entry in feed.entries if (item := self._to_raw(source, entry))]
        return FetchResult(items=items, fetch_state=new_state)

    def _to_raw(self, source: Source, entry: Any) -> RawItem | None:
        link = entry.get("link")
        if not link:
            log.info("rss.entry_without_link", source_id=source.id, entry_id=entry.get("id"))
            return None
        try:
            return RawItem(
                source_id=source.id,
                external_id=str(entry.get("id") or link),
                url=str(link),
                title=str(entry.get("title") or ""),
                text=_entry_text(entry),
                image_url=self._safe_image(entry),
                published_at=entry_published_at(entry),
                raw_meta=_entry_meta(entry),
            )
        except ValidationError as exc:
            log.warning("rss.invalid_entry", source_id=source.id, link=link, error=str(exc))
            return None

    @staticmethod
    def _safe_image(entry: Any) -> str | None:
        url = entry_image_url(entry)
        if url and url.lower().startswith(("http://", "https://")):
            return url
        return None
