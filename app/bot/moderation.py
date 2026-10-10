"""Отправка постов администраторам на модерацию."""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import structlog
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions

from app.bot.keyboards import review_keyboard
from app.config import PublisherConfig
from app.db.models import ProcessedPost
from app.publisher.formatter import PostNews, PostSource, build_post
from app.publisher.limits import FittedPost, fit_post
from app.publisher.media import ImageCheck

log = structlog.get_logger(__name__)

REVIEW_NOTE_PREFIX = "⚠️ Проверьте перед публикацией:"


class ReviewNews(PostNews, Protocol):
    """Новость поста (NewsItem подходит структурно)."""

    source_id: int


class ReviewNewsRepo(Protocol):
    async def get(self, news_id: int) -> ReviewNews | None:
        """Новость по id."""
        raise NotImplementedError


class ReviewSourceRepo(Protocol):
    async def get(self, source_id: int) -> PostSource | None:
        """Источник по id."""
        raise NotImplementedError


class ReviewPostRepo(Protocol):
    async def list_due_reviews(self, now: datetime) -> Sequence[ProcessedPost]:
        """Отложенные посты, срок которых наступил."""
        raise NotImplementedError

    async def set_snooze(self, post_id: int, until: datetime | None) -> bool:
        """Меняет отсрочку поста."""
        raise NotImplementedError


@dataclass(frozen=True)
class ReviewNotifierDeps:
    """Зависимости отправки на модерацию.

    Attributes:
        session_scope: Открывает транзакцию БД.
        news_repo, source_repo, post_repo: Репозитории для сессии.
        admin_ids: Возвращает Telegram ID всех администраторов.
        check_image: Проверка картинки (та же, что при публикации).
        cfg: Секция publisher (лимиты длины поста).
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    news_repo: Callable[[Any], ReviewNewsRepo]
    source_repo: Callable[[Any], ReviewSourceRepo]
    post_repo: Callable[[Any], ReviewPostRepo]
    admin_ids: Callable[[], Awaitable[Sequence[int]]]
    check_image: Callable[[str | None], Awaitable[ImageCheck]]
    cfg: PublisherConfig


@dataclass(frozen=True)
class _Preview:
    fitted: FittedPost
    image_url: str | None
    link: str


class BotReviewNotifier:
    """Реализация ReviewNotifier: предпросмотр поста каждому администратору.

    Предпросмотр собирается тем же build_post и fit_post, что и пост в канале;
    под ним кнопки «Опубликовать», «Изменить», «Отклонить», «Позже».
    """

    def __init__(self, bot: Any, deps: ReviewNotifierDeps) -> None:
        self._bot = bot
        self._deps = deps

    async def notify(self, post: ProcessedPost) -> None:
        """Отправляет пост всем администраторам; ошибка у одного не мешает остальным."""
        preview = await self._build(post)
        if preview is None:
            return
        keyboard = review_keyboard(post.id)
        for admin_id in await self._deps.admin_ids():
            try:
                if post.review_note:
                    note = f"{REVIEW_NOTE_PREFIX} {html.escape(post.review_note)}"
                    await self._bot.send_message(
                        chat_id=admin_id, text=note, parse_mode=ParseMode.HTML
                    )
                await self._send_preview(admin_id, preview, keyboard)
            except Exception:
                log.exception("review.notify_failed", admin_id=admin_id, post_id=post.id)

    async def resend_due(self, now: datetime) -> int:
        """Повторно отправляет отложенные посты, срок которых наступил.

        Returns:
            Сколько постов отправлено.
        """
        async with self._deps.session_scope() as session:
            posts = list(await self._deps.post_repo(session).list_due_reviews(now))
        for post in posts:
            await self.notify(post)
            async with self._deps.session_scope() as session:
                await self._deps.post_repo(session).set_snooze(post.id, None)
        return len(posts)

    async def _build(self, post: ProcessedPost) -> _Preview | None:
        async with self._deps.session_scope() as session:
            news = await self._deps.news_repo(session).get(post.news_item_id)
            source = await self._deps.source_repo(session).get(news.source_id) if news else None
        if news is None or source is None:
            log.error("review.missing_data", post_id=post.id)
            return None
        image = await self._deps.check_image(news.image_url)
        fitted = fit_post(build_post(post, news, source), has_photo=image.ok, cfg=self._deps.cfg)
        return _Preview(fitted=fitted, image_url=news.image_url, link=news.url)

    async def _send_preview(
        self, admin_id: int, preview: _Preview, keyboard: InlineKeyboardMarkup
    ) -> None:
        if preview.fitted.use_photo and preview.image_url:
            try:
                await self._bot.send_photo(
                    chat_id=admin_id,
                    photo=preview.image_url,
                    caption=preview.fitted.html,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                return
            except TelegramBadRequest as exc:
                log.warning("review.photo_rejected", admin_id=admin_id, error=exc.message)
        await self._bot.send_message(
            chat_id=admin_id,
            text=preview.fitted.html,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(url=preview.link, prefer_large_media=True),
            reply_markup=keyboard,
        )
