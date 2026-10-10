"""Нормализация новостей: чистый текст из HTML и канонический URL."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from fnmatch import fnmatchcase
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

HTML_PARSER = "lxml"
PARAGRAPH = "\n\n"
BLOCK_TAGS = (
    "p",
    "div",
    "li",
    "ul",
    "ol",
    "blockquote",
    "pre",
    "section",
    "article",
    "header",
    "footer",
    "tr",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
)
DROP_TAGS = ("script", "style", "noscript", "template")
DEFAULT_PORTS = {"http": 80, "https": 443}

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_WHITESPACE = re.compile(r"\s+")


def clean_html(text: str) -> str:
    """Превращает HTML (или обычный текст) в чистый текст с абзацами.

    Блочные элементы и пустые строки становятся границами абзацев (\\n\\n), <br> —
    пробелом; HTML-сущности раскрываются (в том числе двойное экранирование),
    пробелы, переносы внутри абзаца и неразрывные пробелы схлопываются.

    Args:
        text: HTML или текст.

    Returns:
        Чистый текст; для пустого входа — пустая строка.
    """
    if not text or not text.strip():
        return ""
    soup = BeautifulSoup(text, HTML_PARSER)
    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with(" ")
    for block in soup.find_all(BLOCK_TAGS):
        block.insert_before(PARAGRAPH)
        block.insert_after(PARAGRAPH)
    raw = html.unescape(soup.get_text(separator=""))
    paragraphs = (_WHITESPACE.sub(" ", part).strip() for part in _PARAGRAPH_SPLIT.split(raw))
    return PARAGRAPH.join(p for p in paragraphs if p)


def _drop_param(name: str, patterns: Sequence[str]) -> bool:
    lowered = name.lower()
    return any(fnmatchcase(lowered, pattern.lower()) for pattern in patterns)


def canonical_url(url: str, drop_query_params: Sequence[str]) -> str:
    """Приводит URL к канонической форме для поиска дублей.

    Схема и хост — в нижнем регистре, порт по умолчанию удаляется, фрагмент
    отбрасывается, параметры из drop_query_params (поддерживаются шаблоны вида
    utm_*) удаляются, остальные сортируются; завершающий / удаляется, кроме корня.

    Args:
        url: Исходный адрес.
        drop_query_params: Имена или шаблоны параметров (normalize.drop_query_params).

    Returns:
        Канонический адрес.

    Raises:
        ValueError: У адреса нет схемы или хоста.
    """
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc or not parts.hostname:
        raise ValueError(f"адрес без схемы или хоста: {url!r}")
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parts.port is not None and parts.port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    if parts.username is not None:
        userinfo = parts.username + (f":{parts.password}" if parts.password is not None else "")
        netloc = f"{userinfo}@{netloc}"

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    params = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _drop_param(key, drop_query_params)
    ]
    query = urlencode(sorted(params), quote_via=quote)
    return urlunsplit((scheme, netloc, path, query, ""))
