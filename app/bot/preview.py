"""Предпросмотр поста «ровно как в канале» для модерации и очереди."""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from app.config import PublisherConfig
from app.db.models import ProcessedPost
from app.publisher.formatter import PostNews, PostSource, build_post
from app.publisher.limits import FittedPost, fit_post
from app.publisher.media import ImageCheck, PhotoFetcher
from app.publisher.publisher import as_input_photo

log = structlog.get_logger(__name__)


class ReviewNews(PostNews, Protocol):
    """Новость поста (NewsItem подходит структурно)."""

    source_id: int

    @property
    def raw_meta(self) -> Mapping[str, Any]:
        """Служебные данные сборщика (msg_id и has_photo для фото Telegram)."""
        raise NotImplementedError


class ReviewNewsRepo(Protocol):
    async def get(self, news_id: int) -> ReviewNews | None:
        """Новость по id."""
        raise NotImplementedError


class ReviewSourceRepo(Protocol):
    async def get(self, source_id: int) -> PostSource | None:
        """Источник по id."""
        raise NotImplementedError


@dataclass(frozen=True)
class PostPreview:
    """Собранный пост для показа администратору.

    Attributes:
        fitted: Пост, подогнанный под лимиты, как при публикации.
        photo: Картинка новости: адрес или байты фото из Telegram-источника.
        link: Ссылка на источник; в текст поста не выводится.
    """

    fitted: FittedPost
    photo: str | bytes | None
    link: str


@dataclass(frozen=True)
class PreviewDeps:
    """Зависимости сборки предпросмотра.

    Attributes:
        session_scope: Открывает транзакцию БД.
        news_repo, source_repo: Репозитории для сессии.
        check_image: Проверка картинки (та же, что при публикации).
        cfg: Секция publisher (лимиты длины поста).
        photo_fetcher: Загрузка фото из Telegram-источника; None — только по URL.
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    news_repo: Callable[[Any], ReviewNewsRepo]
    source_repo: Callable[[Any], ReviewSourceRepo]
    check_image: Callable[[str | None], Awaitable[ImageCheck]]
    cfg: PublisherConfig
    photo_fetcher: PhotoFetcher | None = None


async def build_preview(post: ProcessedPost, deps: PreviewDeps) -> PostPreview | None:
    """Собирает пост тем же build_post и fit_post, что и публикация.

    Картинка берётся так же, как при публикации: по URL или, у новостей из
    Telegram, загрузкой фото исходного сообщения — черновик выглядит как пост.

    Returns:
        Предпросмотр или None, если нет новости или источника.
    """
    async with deps.session_scope() as session:
        news = await deps.news_repo(session).get(post.news_item_id)
        source = await deps.source_repo(session).get(news.source_id) if news else None
    if news is None or source is None:
        log.error("review.missing_data", post_id=post.id)
        return None
    photo = await _preview_photo(news, deps)
    fitted = fit_post(
        build_post(post, news, source, deps.cfg), has_photo=photo is not None, cfg=deps.cfg
    )
    return PostPreview(fitted=fitted, photo=photo, link=news.url)


async def _preview_photo(news: ReviewNews, deps: PreviewDeps) -> str | bytes | None:
    """Картинка предпросмотра: проверенный URL или фото из Telegram-источника."""
    if news.image_url:
        image = await deps.check_image(news.image_url)
        return news.image_url if image.ok else None
    if deps.photo_fetcher is None:
        return None
    try:
        return await deps.photo_fetcher.fetch(news)
    except Exception as exc:
        log.warning("review.tg_photo_failed", error=repr(exc))
        return None


async def send_preview(
    bot: Any, chat_id: int, preview: PostPreview, keyboard: InlineKeyboardMarkup | None
) -> Any:
    """Отправляет предпросмотр: фото с подписью, а если Telegram отклонил фото — текстом.

    Returns:
        Отправленное сообщение.
    """
    if preview.fitted.use_photo and preview.photo:
        try:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=as_input_photo(preview.photo),
                caption=preview.fitted.html,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except TelegramBadRequest as exc:
            log.warning("review.photo_rejected", admin_id=chat_id, error=exc.message)
    return await bot.send_message(
        chat_id=chat_id,
        text=preview.fitted.html,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def mark_message(callback: Any, decision: str, who: str) -> None:
    """Убирает кнопки и дописывает решение к сообщению с постом.

    Если отредактировать сообщение нельзя, кнопки убираются, а решение уходит
    отдельным сообщением.
    """
    message = callback.message
    note = f"\n\n<b>{html.escape(decision)}</b> — {html.escape(who)}"
    try:
        if getattr(message, "caption", None) is not None:
            await message.edit_caption(caption=(message.html_text or "") + note, reply_markup=None)
        else:
            await message.edit_text((message.html_text or "") + note, reply_markup=None)
    except TelegramBadRequest as exc:
        log.info("moderation.mark_fallback", error=exc.message)
        await message.edit_reply_markup(reply_markup=None)
        await message.answer(f"{html.escape(decision)} — {html.escape(who)}")
