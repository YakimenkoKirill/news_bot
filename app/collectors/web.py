"""Сборщик сайтов без RSS: страница-список по селекторам и разбор статей."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

import structlog
from pydantic import ValidationError

from app.collectors.base import (
    BaseCollector,
    CollectorError,
    PermanentCollectorError,
    RawItem,
)
from app.collectors.http import HttpClient
from app.collectors.web_article import parse_article
from app.collectors.web_list import ArticleLink, WebSelectors, extract_links
from app.config import CollectorsConfig
from app.db.models import Source

log = structlog.get_logger(__name__)

IsKnownUrl = Callable[[str], Awaitable[bool]]


class RobotsPolicy(Protocol):
    """Проверка robots.txt (RobotsChecker подходит структурно)."""

    async def allowed(self, url: str) -> bool:
        """Разрешена ли загрузка адреса."""
        raise NotImplementedError


class WebCollector(BaseCollector):
    """Собирает новые статьи со страницы-списка сайта.

    Известные ссылки (is_known_url) не загружаются; за один опрос загружается
    не больше max_articles_per_run статей.
    """

    def __init__(
        self,
        http_client: HttpClient,
        robots: RobotsPolicy,
        is_known_url: IsKnownUrl,
        config: CollectorsConfig,
    ) -> None:
        self._http = http_client
        self._robots = robots
        self._is_known_url = is_known_url
        self._max_articles = config.max_articles_per_run
        self._min_chars = config.min_article_chars

    async def fetch(self, source: Source) -> list[RawItem]:
        """Загружает страницу-список и новые статьи источника.

        Args:
            source: Источник с selectors (WebSelectors).

        Returns:
            Новые статьи; статьи с ошибкой или запретом robots.txt пропускаются.

        Raises:
            PermanentCollectorError: Нет или неверные selectors, robots.txt запрещает
                страницу-список, страница отвечает 4xx.
            TemporaryCollectorError: Страница-список временно недоступна.
        """
        selectors = self._selectors(source)
        if not await self._robots.allowed(source.url):
            raise PermanentCollectorError(f"robots.txt запрещает страницу {source.url}")
        page = await self._http.get(source.url)
        links = extract_links(page.body, page.url, selectors)

        fresh: list[ArticleLink] = []
        for link in links:
            if len(fresh) >= self._max_articles:
                break
            if not await self._is_known_url(link.url):
                fresh.append(link)

        items: list[RawItem] = []
        for link in fresh:
            item = await self._fetch_article(source, link)
            if item is not None:
                items.append(item)
        return items

    @staticmethod
    def _selectors(source: Source) -> WebSelectors:
        if not source.selectors:
            raise PermanentCollectorError(f"у источника {source.id} не заданы selectors")
        try:
            return WebSelectors.model_validate(source.selectors)
        except ValidationError as exc:
            raise PermanentCollectorError(
                f"неверные selectors источника {source.id}: {exc}"
            ) from exc

    async def _fetch_article(self, source: Source, link: ArticleLink) -> RawItem | None:
        if not await self._robots.allowed(link.url):
            log.info("web.article_disallowed", source_id=source.id, url=link.url)
            return None
        try:
            response = await self._http.get(link.url)
            article = await asyncio.to_thread(
                parse_article, response.body, response.url, self._min_chars
            )
            if article is None:
                return None
            return RawItem(
                source_id=source.id,
                external_id=link.url,
                url=link.url,
                title=article.title or link.title,
                text=article.text,
                image_url=article.image_url,
                published_at=article.published_at,
            )
        except (CollectorError, ValidationError) as exc:
            log.warning("web.article_failed", source_id=source.id, url=link.url, error=str(exc))
            return None
