"""Команда /errors: журнал ошибок, закрытие инцидентов и повтор упавших новостей."""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.keyboards import (
    ErrorAction,
    ErrorActionType,
    ErrorTab,
    confirm_resolve_all_keyboard,
    errors_keyboard,
    failed_news_keyboard,
    incident_keyboard,
)
from app.db.models import Incident, NewsItem
from app.domain.alerts import AlertKind
from app.pipeline.ai.prompts import truncate_words
from app.pipeline.retry import RetryDeps, RetryOutcome, retry_failed

log = structlog.get_logger(__name__)

MUTED_KINDS_SETTING = "alerts_muted"


class IncidentsRepo(Protocol):
    """Нужная боту часть IncidentRepository."""

    async def get(self, incident_id: int) -> Incident | None:
        """Инцидент по id."""
        raise NotImplementedError

    async def list(self, open_only: bool, limit: int, offset: int) -> Sequence[Incident]:
        """Инциденты, самые свежие первыми."""
        raise NotImplementedError

    async def count(self, open_only: bool) -> int:
        """Число инцидентов."""
        raise NotImplementedError

    async def count_open_by_kind(self) -> dict[str, int]:
        """Число открытых инцидентов по видам."""
        raise NotImplementedError

    async def resolve(self, incident_id: int, by: int, at: datetime) -> bool:
        """Закрывает открытый инцидент."""
        raise NotImplementedError

    async def resolve_all(self, by: int, at: datetime, kind: str | None = None) -> int:
        """Закрывает все открытые инциденты."""
        raise NotImplementedError


class ErrorsNewsRepo(Protocol):
    """Нужная боту часть NewsRepository."""

    async def list_failed(self, limit: int, offset: int) -> Sequence[tuple[NewsItem, bool]]:
        """Новости в статусе failed с признаком «есть пересказ»."""
        raise NotImplementedError

    async def count_failed(self) -> int:
        """Число новостей в статусе failed."""
        raise NotImplementedError

    async def get(self, news_id: int) -> NewsItem | None:
        """Новость по id."""
        raise NotImplementedError


class NamedSource(Protocol):
    """Источник с именем (Source подходит структурно)."""

    name: str


class ErrorsSourceRepo(Protocol):
    """Нужная боту часть SourceRepository."""

    async def get(self, source_id: int) -> NamedSource | None:
        """Источник по id."""
        raise NotImplementedError


class ErrorsSettingsRepo(Protocol):
    """Нужная боту часть SettingsRepository."""

    async def get(self, key: str, default: Any = None) -> Any:
        """Значение настройки."""
        raise NotImplementedError

    async def set(self, key: str, value: Any) -> None:
        """Сохраняет настройку."""
        raise NotImplementedError


@dataclass(frozen=True)
class ErrorsDeps:
    """Зависимости команды /errors (workflow_data: errors_deps).

    Attributes:
        session_scope: Открывает транзакцию БД.
        incident_repo, news_repo, source_repo, settings_repo: Репозитории.
        retry: Зависимости повтора упавших новостей.
        silent_kinds: Виды, отключённые в configs (alerts.silent_kinds).
        page_size: Записей на странице (bot.page_size).
        preview_chars: Длина начала сообщения в списке (bot.error_preview_chars).
        text_limit: Лимит сообщения Telegram (publisher.text_limit).
        tz: Часовой пояс канала (app.timezone).
        clock: Текущее время.
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    incident_repo: Callable[[Any], IncidentsRepo]
    news_repo: Callable[[Any], ErrorsNewsRepo]
    source_repo: Callable[[Any], ErrorsSourceRepo]
    settings_repo: Callable[[Any], ErrorsSettingsRepo]
    retry: RetryDeps
    silent_kinds: frozenset[AlertKind]
    page_size: int
    preview_chars: int
    text_limit: int
    tz: str | ZoneInfo
    clock: Callable[[], datetime]


def _zone(tz: str | ZoneInfo) -> ZoneInfo:
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)


async def _muted_kinds(deps: ErrorsDeps, session: Any) -> set[str]:
    stored = await deps.settings_repo(session).get(MUTED_KINDS_SETTING, [])
    return {str(kind) for kind in stored} if isinstance(stored, list) else set()


def _is_muted(kind: str, muted: set[str], deps: ErrorsDeps) -> bool:
    return kind in muted or kind in {item.value for item in deps.silent_kinds}


def _pages(total: int, page_size: int) -> int:
    return max((total + page_size - 1) // page_size, 1)


async def render_page(
    deps: ErrorsDeps, tab: ErrorTab, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура страницы /errors (страница прижимается к допустимой)."""
    if tab is ErrorTab.NEWS:
        return await _render_news(deps, tab, page)
    return await _render_incidents(deps, tab, page)


async def _render_incidents(
    deps: ErrorsDeps, tab: ErrorTab, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    open_only = tab is ErrorTab.OPEN
    async with deps.session_scope() as session:
        repo = deps.incident_repo(session)
        total = await repo.count(open_only=open_only)
        open_total = await repo.count(open_only=True)
        by_kind = await repo.count_open_by_kind()
        pages = _pages(total, deps.page_size)
        page = min(max(page, 0), pages - 1)
        rows = list(await repo.list(open_only, deps.page_size, page * deps.page_size))

    lines = [texts.ERRORS_TITLE.format(count=open_total)]
    if by_kind:
        summary = " · ".join(
            f"{texts.ALERT_KIND_LABELS[kind]} {count}" for kind, count in sorted(by_kind.items())
        )
        lines.append(summary)
    if total == 0:
        lines.append(texts.ERRORS_EMPTY)
        keyboard = errors_keyboard([], tab, 0, False, False, can_resolve_all=False)
        return "\n".join(lines), keyboard

    lines.append(texts.ERRORS_PAGE.format(page=page + 1, pages=pages))
    lines.append("")
    today = deps.clock().astimezone(_zone(deps.tz))
    numbered: list[tuple[int, int]] = []
    for index, incident in enumerate(rows):
        number = page * deps.page_size + index + 1
        numbered.append((number, incident.id))
        when = texts.error_time(incident.last_seen_at.astimezone(_zone(deps.tz)), today)
        preview = html.escape(truncate_words(incident.message, deps.preview_chars))
        line = texts.incident_line(number, incident.kind, incident.count, when, preview)
        lines.append(line if incident.resolved_at is None else f"{line} ✅")
    keyboard = errors_keyboard(
        numbered,
        tab,
        page,
        has_prev=page > 0,
        has_next=page < pages - 1,
        can_resolve_all=open_only,
    )
    return "\n".join(lines), keyboard


async def _render_news(
    deps: ErrorsDeps, tab: ErrorTab, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    async with deps.session_scope() as session:
        repo = deps.news_repo(session)
        total = await repo.count_failed()
        pages = _pages(total, deps.page_size)
        page = min(max(page, 0), pages - 1)
        rows = list(await repo.list_failed(deps.page_size, page * deps.page_size))
        source_repo = deps.source_repo(session)
        names: dict[int, str] = {}
        for news, _ in rows:
            if news.source_id not in names:
                source = await source_repo.get(news.source_id)
                names[news.source_id] = source.name if source else ""

    lines = [texts.ERRORS_NEWS_TITLE.format(count=total)]
    if total == 0:
        lines.append(texts.ERRORS_NEWS_EMPTY)
        return "\n".join(lines), errors_keyboard([], tab, 0, False, False, can_resolve_all=False)

    lines.append(texts.ERRORS_PAGE.format(page=page + 1, pages=pages))
    lines.append("")
    numbered: list[tuple[int, int]] = []
    for index, (news, _) in enumerate(rows):
        number = page * deps.page_size + index + 1
        numbered.append((number, news.id))
        title = html.escape(truncate_words(news.title, deps.preview_chars))
        source_name = html.escape(names[news.source_id])
        lines.append(texts.failed_news_line(number, title, source_name, news.reject_reason or ""))
    keyboard = errors_keyboard(
        numbered,
        tab,
        page,
        has_prev=page > 0,
        has_next=page < pages - 1,
        can_resolve_all=False,
    )
    return "\n".join(lines), keyboard


def _incident_card(incident: Incident, muted: bool, deps: ErrorsDeps) -> str:
    zone = _zone(deps.tz)
    today = deps.clock().astimezone(zone)
    lines = [
        f"{texts.kind_emoji(incident.kind)} <b>{texts.ALERT_KIND_LABELS[incident.kind]}</b>",
        f"Ключ: <code>{html.escape(incident.key)}</code>",
        f"Первый раз: {texts.error_time(incident.first_seen_at.astimezone(zone), today)}"
        f" · последний раз: {texts.error_time(incident.last_seen_at.astimezone(zone), today)}",
        f"Повторов: {incident.count}",
    ]
    if incident.resolved_at is None:
        lines.append(f"Статус: {texts.ERRORS_OPEN_STATUS}")
    else:
        lines.append(
            "Статус: "
            + texts.ERRORS_RESOLVED_STATUS.format(
                who=incident.resolved_by or "",
                when=texts.error_time(incident.resolved_at.astimezone(zone), today),
            )
        )
    if muted:
        lines.append(texts.ERRORS_MUTED_MARK)
    head = "\n".join(lines)
    budget = max(deps.text_limit - len(head) - 2, 1)
    lines.append("")
    lines.append(html.escape(truncate_words(incident.message, budget)))
    return "\n".join(lines)


async def _news_card(news: NewsItem, has_post: bool, deps: ErrorsDeps) -> str:
    async with deps.session_scope() as session:
        source = await deps.source_repo(session).get(news.source_id)
    reason = news.reject_reason or ""
    lines = [
        f"<b>{html.escape(news.title)}</b>",
        f"Источник: {html.escape(source.name if source else '')}",
        html.escape(news.url),
        f"Причина: {texts.RETRY_REASONS.get(reason, reason)}",
        "",
        texts.RETRY_HINT_TO_QUEUE if has_post else texts.RETRY_HINT_TO_AI,
    ]
    return "\n".join(lines)


async def cmd_errors(message: Any, errors_deps: ErrorsDeps) -> None:
    """Первая страница вкладки «Открытые»."""
    text, keyboard = await render_page(errors_deps, ErrorTab.OPEN, 0)
    await message.answer(text, reply_markup=keyboard)


async def on_error_action(
    callback: Any, callback_data: ErrorAction, errors_deps: ErrorsDeps
) -> None:
    """Кнопки журнала ошибок."""
    deps = errors_deps
    action, tab, page = callback_data.action, callback_data.tab, callback_data.page
    if action is ErrorActionType.PAGE:
        await _edit_list(callback, deps, tab, page)
    elif action is ErrorActionType.BACK:
        text, keyboard = await render_page(deps, tab, page)
        await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()
    elif action is ErrorActionType.OPEN:
        await _open(callback, callback_data, deps)
    elif action is ErrorActionType.RESOLVE:
        await _resolve(callback, callback_data, deps)
    elif action is ErrorActionType.RESOLVE_ALL:
        await callback.message.edit_text(
            texts.ERRORS_CONFIRM_ALL, reply_markup=confirm_resolve_all_keyboard(tab, page)
        )
        await callback.answer()
    elif action is ErrorActionType.CONFIRM_ALL:
        await _resolve_all(callback, deps, tab, page)
    elif action is ErrorActionType.MUTE:
        await _mute(callback, callback_data, deps)
    elif action is ErrorActionType.RETRY:
        await _retry(callback, callback_data, deps)


async def _edit_list(callback: Any, deps: ErrorsDeps, tab: ErrorTab, page: int) -> None:
    text, keyboard = await render_page(deps, tab, page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


async def _open(callback: Any, data: ErrorAction, deps: ErrorsDeps) -> None:
    if data.tab is ErrorTab.NEWS:
        async with deps.session_scope() as session:
            repo = deps.news_repo(session)
            news = await repo.get(data.id)
            rows = await repo.list_failed(1_000, 0)
        if news is None:
            await callback.answer(texts.RETRY_RESULTS["not_found"], show_alert=True)
            return
        has_post = any(item.id == news.id and flag for item, flag in rows)
        card = await _news_card(news, has_post, deps)
        keyboard = failed_news_keyboard(news.id, data.tab, data.page)
        await callback.message.answer(card, reply_markup=keyboard)
        await callback.answer()
        return

    async with deps.session_scope() as session:
        incident = await deps.incident_repo(session).get(data.id)
        muted = await _muted_kinds(deps, session)
    if incident is None:
        await callback.answer(texts.ERRORS_INCIDENT_NOT_FOUND, show_alert=True)
        return
    is_muted = _is_muted(incident.kind, muted, deps)
    keyboard = incident_keyboard(
        incident.id, is_muted, incident.resolved_at is None, data.tab, data.page
    )
    await callback.message.answer(_incident_card(incident, is_muted, deps), reply_markup=keyboard)
    await callback.answer()


async def _resolve(callback: Any, data: ErrorAction, deps: ErrorsDeps) -> None:
    user_id = callback.from_user.id if callback.from_user else 0
    async with deps.session_scope() as session:
        repo = deps.incident_repo(session)
        done = await repo.resolve(data.id, user_id, deps.clock())
        incident = await repo.get(data.id)
        muted = await _muted_kinds(deps, session)
    if not done or incident is None:
        await callback.answer(texts.ERRORS_ALREADY_RESOLVED, show_alert=True)
        return
    log.info("bot.incident_resolved", incident_id=data.id, user_id=user_id)
    is_muted = _is_muted(incident.kind, muted, deps)
    await callback.message.edit_text(
        _incident_card(incident, is_muted, deps),
        reply_markup=incident_keyboard(incident.id, is_muted, False, data.tab, data.page),
    )
    await callback.answer()


async def _resolve_all(callback: Any, deps: ErrorsDeps, tab: ErrorTab, page: int) -> None:
    user_id = callback.from_user.id if callback.from_user else 0
    async with deps.session_scope() as session:
        closed = await deps.incident_repo(session).resolve_all(user_id, deps.clock())
    log.info("bot.incidents_resolved_all", count=closed, user_id=user_id)
    text, keyboard = await render_page(deps, tab, page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(texts.ERRORS_RESOLVED_COUNT.format(count=closed))


async def _mute(callback: Any, data: ErrorAction, deps: ErrorsDeps) -> None:
    async with deps.session_scope() as session:
        incident = await deps.incident_repo(session).get(data.id)
        muted = await _muted_kinds(deps, session)
        if incident is None:
            await callback.answer(texts.ERRORS_INCIDENT_NOT_FOUND, show_alert=True)
            return
        if incident.kind in {item.value for item in deps.silent_kinds}:
            await callback.answer(texts.ERRORS_KIND_FROM_CONFIG, show_alert=True)
            return
        if incident.kind in muted:
            muted.discard(incident.kind)
        else:
            muted.add(incident.kind)
        await deps.settings_repo(session).set(MUTED_KINDS_SETTING, sorted(muted))
    is_muted = incident.kind in muted
    log.info("bot.alert_kind_muted", kind=incident.kind, muted=is_muted)
    await callback.message.edit_text(
        _incident_card(incident, is_muted, deps),
        reply_markup=incident_keyboard(
            incident.id, is_muted, incident.resolved_at is None, data.tab, data.page
        ),
    )
    await callback.answer()


async def _retry(callback: Any, data: ErrorAction, deps: ErrorsDeps) -> None:
    result = await retry_failed(data.id, deps.retry)
    status = result.status.value if result.status is not None else ""
    text = texts.RETRY_RESULTS[result.outcome.value].format(status=status)
    if result.outcome in (RetryOutcome.TO_AI, RetryOutcome.TO_QUEUE):
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text)
    await callback.answer()


def create_router() -> Router:
    """Роутер журнала ошибок."""
    router = Router(name="errors")
    router.message.register(cmd_errors, Command("errors"))
    router.callback_query.register(on_error_action, ErrorAction.filter())
    return router
