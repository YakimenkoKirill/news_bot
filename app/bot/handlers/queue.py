"""Команда /queue: очередь подготовленных постов и посты на модерации."""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.keyboards import (
    QUEUE_TAB_STATUSES,
    QueueAction,
    QueueActionType,
    QueueTab,
    queue_item_keyboard,
    queue_keyboard,
)
from app.bot.moderation import REVIEW_NOTE_PREFIX
from app.bot.preview import PreviewDeps, build_preview, send_preview
from app.config import PublisherConfig
from app.db.models import NewsItem, ProcessedPost
from app.domain.enums import NewsStatus
from app.pipeline.ai.prompts import truncate_words
from app.publisher.slots import queue_eta
from app.publisher.stage_publish import PAUSED_SETTING

log = structlog.get_logger(__name__)


class QueueNews(Protocol):
    """Новость в очереди (NewsItem подходит структурно)."""

    id: int
    source_id: int
    status: NewsStatus


class QueueNewsRepo(Protocol):
    """Нужная боту часть NewsRepository."""

    async def list_prepared(
        self,
        statuses: Sequence[NewsStatus],
        decay_per_h: float,
        limit: int,
        offset: int,
    ) -> Sequence[tuple[NewsItem, ProcessedPost]]:
        """Новости с пересказом по приоритету публикации."""
        raise NotImplementedError

    async def count_prepared(self, statuses: Sequence[NewsStatus]) -> int:
        """Число новостей с пересказом в статусах."""
        raise NotImplementedError

    async def get(self, news_id: int) -> QueueNews | None:
        """Новость по id."""
        raise NotImplementedError


class QueuePostRepo(Protocol):
    """Нужная боту часть ProcessedPostRepository."""

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        """Пересказ новости."""
        raise NotImplementedError


class QueuePublicationRepo(Protocol):
    """Нужная боту часть PublicationRepository."""

    async def last_published_at(self, channel_id: int) -> datetime | None:
        """Время последней успешной публикации."""
        raise NotImplementedError


class QueueSettingsRepo(Protocol):
    """Нужная боту часть SettingsRepository."""

    async def get(self, key: str, default: Any = None) -> Any:
        """Значение настройки."""
        raise NotImplementedError


class NamedSource(Protocol):
    """Источник с именем (Source подходит структурно)."""

    name: str


class QueueSourceRepo(Protocol):
    """Нужная боту часть SourceRepository."""

    async def get(self, source_id: int) -> NamedSource | None:
        """Источник по id."""
        raise NotImplementedError


@dataclass(frozen=True)
class QueueDeps:
    """Зависимости команды /queue (workflow_data: queue_deps).

    Attributes:
        session_scope: Открывает транзакцию БД.
        news_repo, post_repo, publication_repo, settings_repo, source_repo: Репозитории.
        preview: Зависимости предпросмотра поста.
        publisher_cfg: Секция publisher (интервал, тихие часы, score_decay_per_h).
        channel_id: Канал публикации.
        tz: Часовой пояс канала (app.timezone).
        page_size: Постов на странице (bot.page_size).
        title_chars: Длина заголовка в списке (bot.queue_title_chars).
        clock: Текущее время.
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    news_repo: Callable[[Any], QueueNewsRepo]
    post_repo: Callable[[Any], QueuePostRepo]
    publication_repo: Callable[[Any], QueuePublicationRepo]
    settings_repo: Callable[[Any], QueueSettingsRepo]
    source_repo: Callable[[Any], QueueSourceRepo]
    preview: PreviewDeps
    publisher_cfg: PublisherConfig
    channel_id: int
    tz: str | ZoneInfo
    page_size: int
    title_chars: int
    clock: Callable[[], datetime]


def _zone(tz: str | ZoneInfo) -> ZoneInfo:
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)


async def render_page(
    deps: QueueDeps, tab: QueueTab, page: int, now: datetime
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура страницы /queue.

    Посты идут в том же порядке, в котором их выбирает publish_due, поэтому номер
    в списке — номер в очереди выхода. Страница прижимается к допустимой. Для
    вкладки «В очереди» показывается ожидаемое время выхода (кроме паузы).

    Args:
        deps: Зависимости.
        tab: Вкладка.
        page: Запрошенная страница (с нуля).
        now: Текущий момент.

    Returns:
        HTML-текст и клавиатура.
    """
    statuses = QUEUE_TAB_STATUSES[tab]
    cfg = deps.publisher_cfg
    async with deps.session_scope() as session:
        news_repo = deps.news_repo(session)
        total = await news_repo.count_prepared(statuses)
        if total == 0:
            text = f"{texts.QUEUE_TITLES[tab.value]}\n{texts.QUEUE_EMPTY[tab.value]}"
            return text, queue_keyboard([], tab, 0, has_prev=False, has_next=False)
        pages = (total + deps.page_size - 1) // deps.page_size
        page = min(max(page, 0), pages - 1)
        offset = page * deps.page_size
        rows = list(
            await news_repo.list_prepared(statuses, cfg.score_decay_per_h, deps.page_size, offset)
        )
        source_repo = deps.source_repo(session)
        names: dict[int, str] = {}
        for news, _ in rows:
            if news.source_id not in names:
                source = await source_repo.get(news.source_id)
                names[news.source_id] = source.name if source else ""
        paused = bool(await deps.settings_repo(session).get(PAUSED_SETTING, False))
        last = await deps.publication_repo(session).last_published_at(deps.channel_id)

    lines = [texts.queue_header(tab.value, total, page + 1, pages)]
    if tab is QueueTab.QUEUE:
        lines.append(texts.QUEUE_HINT)
        if paused:
            lines.append(texts.QUEUE_PAUSED)
    lines.append("")
    etas: list[datetime | None] = [None] * len(rows)
    if tab is QueueTab.QUEUE and not paused:
        all_etas = queue_eta(
            now, last, offset + len(rows), cfg.min_interval_min, cfg.quiet_window, deps.tz
        )
        etas = list(all_etas[offset:])
    zone = _zone(deps.tz)
    today = now.astimezone(zone)
    positions: list[tuple[int, int]] = []
    for index, ((news, post), eta) in enumerate(zip(rows, etas, strict=True)):
        number = offset + index + 1
        positions.append((number, news.id))
        title = html.escape(truncate_words(post.ai_title, deps.title_chars))
        source_name = html.escape(names[news.source_id])
        score = post.relevance_score
        if tab is QueueTab.QUEUE:
            eta_text = texts.queue_eta_text(eta.astimezone(zone), today) if eta else None
            lines.append(texts.queue_line(number, title, source_name, score, eta_text))
        else:
            has_note = bool(post.review_note)
            lines.append(texts.review_line(number, title, source_name, score, has_note))
    keyboard = queue_keyboard(positions, tab, page, has_prev=page > 0, has_next=page < pages - 1)
    return "\n".join(lines), keyboard


async def cmd_queue(message: Any, queue_deps: QueueDeps) -> None:
    """Первая страница вкладки «В очереди»."""
    text, keyboard = await render_page(queue_deps, QueueTab.QUEUE, 0, queue_deps.clock())
    await message.answer(text, reply_markup=keyboard)


async def on_queue_action(
    callback: Any, callback_data: QueueAction, queue_deps: QueueDeps, bot: Any
) -> None:
    """Кнопки списка /queue: страница или вкладка, предпросмотр поста, возврат к списку."""
    deps = queue_deps
    tab, page = callback_data.tab, callback_data.page
    if callback_data.action is QueueActionType.PAGE:
        text, keyboard = await render_page(deps, tab, page, deps.clock())
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()
        return
    if callback_data.action is QueueActionType.BACK:
        text, keyboard = await render_page(deps, tab, page, deps.clock())
        await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()
        return
    await _open(callback, callback_data, deps, bot)


async def _open(callback: Any, data: QueueAction, deps: QueueDeps, bot: Any) -> None:
    async with deps.session_scope() as session:
        news = await deps.news_repo(session).get(data.news_id)
        post = await deps.post_repo(session).get_by_news(data.news_id) if news else None
    if news is None or post is None or news.status not in QUEUE_TAB_STATUSES[data.tab]:
        status = news.status.value if news is not None else texts.QUEUE_DELETED
        await callback.answer(texts.QUEUE_NOT_IN_QUEUE.format(status=status), show_alert=True)
        return
    preview = await build_preview(post, deps.preview)
    if preview is None:
        await callback.answer(texts.REVIEW_POST_NOT_FOUND, show_alert=True)
        return
    chat_id = callback.from_user.id
    if post.review_note:
        note = f"{REVIEW_NOTE_PREFIX} {html.escape(post.review_note)}"
        await bot.send_message(chat_id=chat_id, text=note, parse_mode=ParseMode.HTML)
    keyboard = queue_item_keyboard(news.id, data.tab, data.page)
    await send_preview(bot, chat_id, preview, keyboard)
    log.info("bot.queue_preview", news_id=news.id, user_id=chat_id)
    await callback.answer()


def create_router() -> Router:
    """Роутер команды /queue."""
    router = Router(name="queue")
    router.message.register(cmd_queue, Command("queue"))
    actions = {QueueActionType.PAGE, QueueActionType.OPEN, QueueActionType.BACK}
    router.callback_query.register(on_queue_action, QueueAction.filter(F.action.in_(actions)))
    return router
