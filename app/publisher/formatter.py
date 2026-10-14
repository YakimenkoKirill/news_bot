"""Сборка поста для Telegram в режиме HTML."""

from __future__ import annotations

import html
from dataclasses import dataclass, field, replace
from typing import Protocol

PARAGRAPH = "\n\n"


class PostContent(Protocol):
    """Пересказ модели (ProcessedPost подходит структурно)."""

    ai_title: str
    ai_summary: str
    hashtags: list[str]


class PostNews(Protocol):
    """Новость (NewsItem подходит структурно)."""

    url: str
    image_url: str | None


class PostSource(Protocol):
    """Источник (Source подходит структурно)."""

    name: str


def escape(text: str) -> str:
    """Экранирует текст и атрибуты для parse_mode=HTML."""
    return html.escape(text, quote=True)


@dataclass(frozen=True)
class Post:
    """Пост для канала: части хранятся как обычный текст и экранируются при сборке.

    Attributes:
        title: Заголовок (выводится жирным).
        summary: Пересказ.
        hashtags: Хэштеги.
        link: Адрес источника; в текст поста не выводится.
        source_name: Имя источника; в текст поста не выводится.
        image_url: Картинка новости.
    """

    title: str
    summary: str
    link: str
    source_name: str
    hashtags: list[str] = field(default_factory=list)
    image_url: str | None = None

    @property
    def hashtags_line(self) -> str:
        """Строка хэштегов через пробел (пустая, если хэштегов нет)."""
        return " ".join(self.hashtags)

    @property
    def html(self) -> str:
        """Готовый текст поста: заголовок и пересказ; используется только тег b."""
        parts = [f"<b>{escape(self.title)}</b>", escape(self.summary)]
        if self.hashtags:
            parts.append(escape(self.hashtags_line))
        return PARAGRAPH.join(part for part in parts if part)

    @property
    def visible_length(self) -> int:
        """Длина текста поста, как её считает Telegram (без HTML-разметки)."""
        parts = [self.title, self.summary]
        if self.hashtags:
            parts.append(self.hashtags_line)
        return len(PARAGRAPH.join(part for part in parts if part))

    def with_summary(self, summary: str) -> Post:
        """Копия поста с другим пересказом."""
        return replace(self, summary=summary)


def build_post(processed: PostContent, news: PostNews, source: PostSource) -> Post:
    """Собирает пост из пересказа, новости и источника.

    Ссылка на источник и его имя сохраняются в Post, но в текст поста не попадают.

    Args:
        processed: Пересказ модели.
        news: Исходная новость (ссылка и картинка).
        source: Источник (имя).

    Returns:
        Пост с отдельными частями для подрезки по длине.
    """
    return Post(
        title=processed.ai_title.strip(),
        summary=processed.ai_summary.strip(),
        hashtags=[tag for tag in processed.hashtags if tag.strip()],
        link=news.url,
        source_name=source.name.strip(),
        image_url=news.image_url,
    )
