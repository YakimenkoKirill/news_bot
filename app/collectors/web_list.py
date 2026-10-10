"""Извлечение ссылок на статьи со страницы-списка сайта по CSS-селекторам."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

import soupsieve
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, field_validator

ALLOWED_SCHEMES = frozenset({"http", "https"})
HTML_PARSER = "lxml"


class WebSelectors(BaseModel):
    """CSS-селекторы страницы-списка (поле sources.selectors).

    Attributes:
        list_item: Элемент одной статьи в списке.
        link: Ссылка внутри элемента (сам элемент, если это <a>).
        title: Заголовок внутри элемента; если не задан — текст ссылки.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    list_item: str
    link: str = "a"
    title: str | None = None

    @field_validator("list_item", "link", "title")
    @classmethod
    def _check_selector(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("пустой CSS-селектор")
        try:
            soupsieve.compile(value)
        except soupsieve.SelectorSyntaxError as exc:
            raise ValueError(f"невалидный CSS-селектор {value!r}: {exc}") from exc
        return value


@dataclass(frozen=True)
class ArticleLink:
    """Ссылка на статью со страницы-списка."""

    url: str
    title: str


def _text(tag: Tag | None) -> str:
    return " ".join(tag.get_text(" ").split()) if tag is not None else ""


def _find_link(item: Tag, selectors: WebSelectors) -> Tag | None:
    found = item.select_one(selectors.link)
    if found is not None:
        return found
    return item if item.name == "a" else None


def extract_links(html: str | bytes, base_url: str, selectors: WebSelectors) -> list[ArticleLink]:
    """Извлекает ссылки на статьи в порядке страницы.

    Относительные адреса превращаются в абсолютные через urljoin; ссылки на схемы,
    кроме http(s), и элементы без ссылки пропускаются; повторы (в том числе
    отличающиеся только якорем) отбрасываются.

    Args:
        html: HTML страницы-списка.
        base_url: Адрес страницы для разрешения относительных ссылок.
        selectors: Селекторы источника.

    Returns:
        Ссылки с заголовками без повторов.
    """
    soup = BeautifulSoup(html, HTML_PARSER)
    links: list[ArticleLink] = []
    seen: set[str] = set()
    for item in soup.select(selectors.list_item):
        link_tag = _find_link(item, selectors)
        href = link_tag.get("href") if link_tag is not None else None
        if not isinstance(href, str) or not href.strip():
            continue
        url, _fragment = urldefrag(urljoin(base_url, href.strip()))
        parts = urlsplit(url)
        if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.netloc or url in seen:
            continue
        title = _text(item.select_one(selectors.title)) if selectors.title else ""
        links.append(ArticleLink(url=url, title=title or _text(link_tag)))
        seen.add(url)
    return links
