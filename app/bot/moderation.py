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

from app.bot.keyboards import review_keyboard
from app.bot.preview import (
    PreviewDeps,
    ReviewNews,
    ReviewNewsRepo,
    ReviewSourceRepo,
    build_preview,
    send_preview,
)
from app.config import PublisherConfig
from app.db.models import ProcessedPost
from app.publisher.media import ImageCheck

log = structlog.get_logger(__name__)

__all__ = [
    "REVIEW_NOTE_PREFIX",
    "BotReviewNotifier",
    "ReviewNews",
    "ReviewNewsRepo",
    "ReviewNotifierDeps",
    "ReviewPostRepo",
    "ReviewSourceRepo",
]

REVIEW_NOTE_PREFIX = "⚠️ Проверьте перед публикацией:"


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


class BotReviewNotifier:
    """Реализация ReviewNotifier: предпросмотр поста каждому администратору.

    Предпросмотр собирается тем же build_post и fit_post, что и пост в канале;
    под ним кнопки «Опубликовать», «Изменить», «Отклонить», «Позже».
    """

    def __init__(self, bot: Any, deps: ReviewNotifierDeps) -> None:
        self._bot = bot
        self._deps = deps
        self._preview_deps = PreviewDeps(
            session_scope=deps.session_scope,
            news_repo=deps.news_repo,
            source_repo=deps.source_repo,
            check_image=deps.check_image,
            cfg=deps.cfg,
        )

    async def notify(self, post: ProcessedPost) -> None:
        """Отправляет пост всем администраторам; ошибка у одного не мешает остальным."""
        preview = await build_preview(post, self._preview_deps)
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
                await send_preview(self._bot, admin_id, preview, keyboard)
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
