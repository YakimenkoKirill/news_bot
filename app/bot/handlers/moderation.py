"""Решения модератора по кнопкам под постом."""

from __future__ import annotations

import html
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog
from aiogram import Router
from aiogram.fsm.context import FSMContext

from app.bot import texts
from app.bot.keyboards import ReviewAction, ReviewActionType
from app.bot.preview import mark_message
from app.bot.states import EditPost
from app.config import PublisherConfig
from app.db.models import ProcessedPost
from app.db.repositories.news import StaleStatusError
from app.domain.enums import NewsStatus
from app.pipeline.queue_limit import QueueAdmission
from app.publisher.formatter import PostNews, PostSource, build_post
from app.publisher.limits import summary_budget, utf16_len

log = structlog.get_logger(__name__)

REJECT_REASON = "rejected_by_moderator"


class ModeratedNews(PostNews, Protocol):
    """Новость поста (NewsItem подходит структурно)."""

    id: int
    source_id: int
    status: NewsStatus


class ModerationNewsRepo(Protocol):
    async def get(self, news_id: int) -> ModeratedNews | None:
        """Новость по id."""
        raise NotImplementedError

    async def set_status(
        self,
        news_id: int,
        expected: NewsStatus,
        new: NewsStatus,
        reason: str | None = None,
    ) -> None:
        """Смена статуса через машину состояний."""
        raise NotImplementedError


class ModerationPostRepo(Protocol):
    async def get(self, post_id: int) -> ProcessedPost | None:
        """Пост по id."""
        raise NotImplementedError

    async def update_summary(self, post_id: int, summary: str, edited_by: int) -> bool:
        """Замена пересказа."""
        raise NotImplementedError

    async def set_snooze(self, post_id: int, until: datetime | None) -> bool:
        """Отсрочка повторного показа."""
        raise NotImplementedError


class ModerationSourceRepo(Protocol):
    async def get(self, source_id: int) -> PostSource | None:
        """Источник по id."""
        raise NotImplementedError


@dataclass(frozen=True)
class ModerationDeps:
    """Зависимости обработки решений (workflow_data: moderation_deps)."""

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    news_repo: Callable[[Any], ModerationNewsRepo]
    post_repo: Callable[[Any], ModerationPostRepo]
    source_repo: Callable[[Any], ModerationSourceRepo]
    cfg: PublisherConfig
    snooze_min: float
    admission: QueueAdmission | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def _who(event: Any) -> tuple[int | None, str]:
    user = getattr(event, "from_user", None)
    if user is None:
        return None, "неизвестно"
    name = f"@{user.username}" if getattr(user, "username", None) else str(user.id)
    return user.id, name


async def _load(deps: ModerationDeps, post_id: int) -> tuple[ProcessedPost, ModeratedNews] | None:
    async with deps.session_scope() as session:
        post = await deps.post_repo(session).get(post_id)
        news = await deps.news_repo(session).get(post.news_item_id) if post else None
    if post is None or news is None:
        return None
    return post, news


async def on_review(
    callback: Any,
    callback_data: ReviewAction,
    state: FSMContext,
    moderation_deps: ModerationDeps,
) -> None:
    """Кнопки «Опубликовать», «Отклонить», «Позже», «Изменить»."""
    deps = moderation_deps
    loaded = await _load(deps, callback_data.post_id)
    if loaded is None:
        await callback.answer(texts.REVIEW_POST_NOT_FOUND, show_alert=True)
        return
    post, news = loaded
    if news.status is not NewsStatus.PENDING_REVIEW:
        done = texts.REVIEW_ALREADY_DONE.format(status=news.status.value)
        await callback.answer(done, show_alert=True)
        return
    user_id, who = _who(callback)
    action = callback_data.action

    if action is ReviewActionType.EDIT:
        await state.set_state(EditPost.summary)
        await state.update_data(post_id=post.id)
        await callback.answer()
        limit = await _limit(deps, post, news)
        await callback.message.answer(texts.REVIEW_ASK_SUMMARY.format(limit=limit))
        return

    if action is ReviewActionType.LATER:
        until = deps.clock() + timedelta(minutes=deps.snooze_min)
        async with deps.session_scope() as session:
            await deps.post_repo(session).set_snooze(post.id, until)
        decision = texts.REVIEW_DECISIONS["later"].format(minutes=int(deps.snooze_min))
    else:
        new = NewsStatus.APPROVED if action is ReviewActionType.PUBLISH else NewsStatus.REJECTED
        reason = REJECT_REASON if new is NewsStatus.REJECTED else None
        displaced: int | None = None
        try:
            async with deps.session_scope() as session:
                if new is NewsStatus.APPROVED and deps.admission is not None:
                    admitted = await deps.admission.admit(
                        session,
                        news.id,
                        news.source_id,
                        NewsStatus.PENDING_REVIEW,
                        forced=True,
                    )
                    displaced = admitted.displaced_id
                else:
                    await deps.news_repo(session).set_status(
                        news.id, NewsStatus.PENDING_REVIEW, new, reason
                    )
        except StaleStatusError:
            current = await _load(deps, post.id)
            status = current[1].status.value if current else "неизвестно"
            await callback.answer(texts.REVIEW_ALREADY_DONE.format(status=status), show_alert=True)
            return
        decision = texts.REVIEW_DECISIONS[action.value]
        if displaced is not None:
            decision = f"{decision}{texts.REVIEW_DISPLACED}"
    log.info("moderation.decision", post_id=post.id, action=action.value, user_id=user_id)
    await callback.answer()
    await mark_message(callback, decision, who)


async def _limit(deps: ModerationDeps, post: ProcessedPost, news: ModeratedNews) -> int:
    async with deps.session_scope() as session:
        source = await deps.source_repo(session).get(news.source_id)
    if source is None:
        return deps.cfg.text_limit
    built = build_post(post, news, source, deps.cfg)
    limit = deps.cfg.caption_limit if news.image_url else deps.cfg.text_limit
    return max(summary_budget(built, limit), 0)


async def on_new_summary(message: Any, state: FSMContext, moderation_deps: ModerationDeps) -> None:
    """Новый текст пересказа: проверка длины, сохранение и одобрение."""
    deps = moderation_deps
    text = (message.text or "").strip()
    if not text:
        await message.answer(texts.REVIEW_SUMMARY_EMPTY)
        return
    post_id = int((await state.get_data())["post_id"])
    loaded = await _load(deps, post_id)
    if loaded is None:
        await state.clear()
        await message.answer(texts.REVIEW_POST_NOT_FOUND)
        return
    post, news = loaded
    limit = await _limit(deps, post, news)
    if utf16_len(text) > limit:
        await message.answer(texts.REVIEW_SUMMARY_TOO_LONG.format(limit=limit))
        return
    user_id, who = _who(message)
    try:
        async with deps.session_scope() as session:
            await deps.post_repo(session).update_summary(post.id, text, edited_by=user_id or 0)
            if deps.admission is not None:
                await deps.admission.admit(
                    session, news.id, news.source_id, NewsStatus.PENDING_REVIEW, forced=True
                )
            else:
                await deps.news_repo(session).set_status(
                    news.id, NewsStatus.PENDING_REVIEW, NewsStatus.APPROVED
                )
    except StaleStatusError:
        await state.clear()
        current = await _load(deps, post.id)
        status = current[1].status.value if current else "неизвестно"
        await message.answer(texts.REVIEW_ALREADY_DONE.format(status=status))
        return
    await state.clear()
    log.info("moderation.edited", post_id=post.id, user_id=user_id)
    await message.answer(f"{texts.REVIEW_EDITED} ({html.escape(who)})")


def create_router() -> Router:
    """Роутер решений модератора."""
    router = Router(name="moderation")
    router.callback_query.register(on_review, ReviewAction.filter())
    router.message.register(on_new_summary, EditPost.summary)
    return router
