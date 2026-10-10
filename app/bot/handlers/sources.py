"""Список источников и действия с ними через inline-кнопки."""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from aiogram import Router
from aiogram.filters import Command

from app.bot import texts
from app.bot.keyboards import (
    SourceAction,
    SourceActionType,
    confirm_delete_keyboard,
    sources_keyboard,
)
from app.collectors.base import BaseCollector
from app.db.models import Source
from app.domain.enums import SourceType

log = structlog.get_logger(__name__)


class SourcesRepo(Protocol):
    """Нужная боту часть SourceRepository."""

    async def list_all(self) -> Sequence[Source]:
        """Все источники."""
        raise NotImplementedError

    async def get(self, source_id: int) -> Source | None:
        """Источник по id."""
        raise NotImplementedError

    async def set_enabled(self, source_id: int, enabled: bool) -> bool:
        """Включает или выключает источник."""
        raise NotImplementedError

    async def delete(self, source_id: int) -> bool:
        """Удаляет источник."""
        raise NotImplementedError


@dataclass(frozen=True)
class SourcesDeps:
    """Зависимости управления источниками (workflow_data: sources_deps).

    Attributes:
        session_scope: Открывает транзакцию БД.
        source_repo: Репозиторий источников для сессии.
        collectors: Сборщики для пробного сбора по типу источника.
        reload_telegram: Перерегистрирует подписки TelegramCollector (или None).
        page_size: Источников на странице (bot.page_size).
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    source_repo: Callable[[Any], SourcesRepo]
    collectors: Mapping[SourceType, BaseCollector]
    reload_telegram: Callable[[], Awaitable[None]] | None
    page_size: int


def _source_line(source: Source) -> str:
    state = "вкл" if source.enabled else "выкл"
    errors = f", ошибок подряд: {source.error_count}" if source.error_count else ""
    return f"#{source.id} [{source.type.value}] {html.escape(source.name)} — {state}{errors}"


async def render_page(deps: SourcesDeps, page: int) -> tuple[str, Any]:
    """Текст и клавиатура страницы источников (страница прижимается к допустимой)."""
    async with deps.session_scope() as session:
        sources = list(await deps.source_repo(session).list_all())
    if not sources:
        return texts.NO_SOURCES, None
    pages = (len(sources) + deps.page_size - 1) // deps.page_size
    page = min(max(page, 0), pages - 1)
    chunk = sources[page * deps.page_size : (page + 1) * deps.page_size]
    lines = [f"<b>Источники</b> (страница {page + 1} из {pages})"]
    lines.extend(_source_line(source) for source in chunk)
    keyboard = sources_keyboard(chunk, page, has_prev=page > 0, has_next=page < pages - 1)
    return "\n".join(lines), keyboard


async def cmd_sources(message: Any, sources_deps: SourcesDeps) -> None:
    """Первая страница списка источников."""
    text, keyboard = await render_page(sources_deps, 0)
    await message.answer(text, reply_markup=keyboard)


async def _show_page(callback: Any, deps: SourcesDeps, page: int) -> None:
    text, keyboard = await render_page(deps, page)
    await callback.message.edit_text(text, reply_markup=keyboard)


async def _reload_if_telegram(source: Source, deps: SourcesDeps) -> None:
    if source.type is SourceType.TG and deps.reload_telegram is not None:
        await deps.reload_telegram()


async def on_source_action(
    callback: Any, callback_data: SourceAction, sources_deps: SourcesDeps
) -> None:
    """Обработка кнопок списка источников."""
    deps = sources_deps
    action, page = callback_data.action, callback_data.page
    if action is SourceActionType.PAGE:
        await _show_page(callback, deps, page)
        await callback.answer()
        return

    async with deps.session_scope() as session:
        source = await deps.source_repo(session).get(callback_data.id)
    if source is None:
        await callback.answer(texts.SOURCE_NOT_FOUND, show_alert=True)
        await _show_page(callback, deps, page)
        return

    user_id = callback.from_user.id if callback.from_user else None
    if action is SourceActionType.TOGGLE:
        async with deps.session_scope() as session:
            await deps.source_repo(session).set_enabled(source.id, not source.enabled)
        log.info(
            "bot.source_toggled",
            source_id=source.id,
            enabled=not source.enabled,
            user_id=user_id,
        )
        await _reload_if_telegram(source, deps)
        await _show_page(callback, deps, page)
        await callback.answer()
    elif action is SourceActionType.DELETE:
        await callback.message.edit_text(
            texts.confirm_delete(html.escape(source.name)),
            reply_markup=confirm_delete_keyboard(source.id, page),
        )
        await callback.answer()
    elif action is SourceActionType.CONFIRM_DELETE:
        async with deps.session_scope() as session:
            await deps.source_repo(session).delete(source.id)
        log.info("bot.source_deleted", source_id=source.id, user_id=user_id)
        await _reload_if_telegram(source, deps)
        await _show_page(callback, deps, page)
        await callback.answer(texts.SOURCE_DELETED)
    elif action is SourceActionType.CHECK:
        await callback.answer()
        await callback.message.answer(await check_source(source, deps))


async def check_source(source: Source, deps: SourcesDeps) -> str:
    """Пробный сбор без сохранения: сколько материалов нашлось или какая ошибка."""
    collector = deps.collectors.get(source.type)
    if collector is None:
        return texts.CHECK_UNSUPPORTED
    try:
        items = await collector.fetch(source)
    except Exception as exc:
        log.warning("bot.source_check_failed", source_id=source.id, error=repr(exc))
        return f"Проверка #{source.id}: ошибка — {html.escape(str(exc))}"
    lines = [f"Проверка #{source.id}: найдено материалов — {len(items)}"]
    if items:
        lines.append(f"Первый: {html.escape(items[0].title or items[0].url)}")
    return "\n".join(lines)


def create_router() -> Router:
    """Роутер управления источниками."""
    router = Router(name="sources")
    router.message.register(cmd_sources, Command("sources"))
    router.callback_query.register(on_source_action, SourceAction.filter())
    return router
