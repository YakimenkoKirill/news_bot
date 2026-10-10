"""Диалог добавления источника (/add_source)."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.bot import texts
from app.bot.keyboards import (
    ADD_FIELD_CONFIRM,
    ADD_FIELD_MODERATION,
    ADD_FIELD_TYPE,
    CONFIRM_CHECK,
    CONFIRM_SAVE,
    AddSourceChoice,
    choice_keyboard,
)
from app.bot.states import AddSource
from app.collectors.base import BaseCollector
from app.collectors.web_list import WebSelectors
from app.db.models import Source
from app.domain.enums import ModerationMode, SourceType

log = structlog.get_logger(__name__)

TME_URL = "https://t.me/{username}"
NAME_MAX_CHARS = 255
TRIAL_SOURCE_ID = 1
TRIAL_TITLES = 3
HTTP_SCHEMES = frozenset({"http", "https"})
_TG_CHANNEL = re.compile(
    r"^(?:@|(?:https?://)?(?:t|telegram)\.me/)?([A-Za-z][A-Za-z0-9_]{4,31})/?$"
)

TYPE_OPTIONS = [(t.value, t.value) for t in SourceType]
MODERATION_OPTIONS = [
    (ModerationMode.AUTO.value, "Сразу (auto)"),
    (ModerationMode.REVIEW.value, "На модерацию (review)"),
]
CONFIRM_OPTIONS = [(CONFIRM_CHECK, "Пробный сбор"), (CONFIRM_SAVE, "Сохранить")]


class AddSourceRepo(Protocol):
    """Нужная диалогу часть SourceRepository."""

    async def list_all(self) -> Sequence[Source]:
        """Все источники."""
        raise NotImplementedError

    async def add(self, source: Source) -> Source:
        """Сохраняет источник."""
        raise NotImplementedError


@dataclass(frozen=True)
class SourceAddDeps:
    """Зависимости диалога (workflow_data: source_add_deps)."""

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    source_repo: Callable[[Any], AddSourceRepo]
    collectors: Mapping[SourceType, BaseCollector]
    reload_telegram: Callable[[], Awaitable[None]] | None
    min_interval_min: int


def normalize_address(source_type: SourceType, raw: str) -> str | None:
    """Адрес источника в каноническом виде или None, если ввод неверный.

    rss и web — http(s) URL; tg — @username или t.me/username, сохраняется как
    https://t.me/username.
    """
    value = raw.strip()
    if source_type is SourceType.TG:
        match = _TG_CHANNEL.match(value)
        return TME_URL.format(username=match.group(1)) if match else None
    parts = urlsplit(value)
    if parts.scheme.lower() in HTTP_SCHEMES and parts.netloc:
        return value
    return None


def _source_from_state(data: Mapping[str, Any], source_id: int | None = None) -> Source:
    return Source(
        id=source_id,
        type=SourceType(data["type"]),
        url=data["url"],
        name=data["name"],
        interval_min=data["interval"],
        enabled=True,
        moderation_mode=ModerationMode(data["moderation"]),
        selectors=data.get("selectors"),
        fetch_state={},
        error_count=0,
    )


def _summary(data: Mapping[str, Any]) -> str:
    lines = [
        "<b>Новый источник</b>",
        f"Тип: {data['type']}",
        f"Адрес: {html.escape(data['url'])}",
        f"Имя: {html.escape(data['name'])}",
        f"Интервал: {data['interval']} мин",
        f"Модерация: {data['moderation']}",
    ]
    if data.get("selectors"):
        lines.append(f"Селекторы: <code>{html.escape(json.dumps(data['selectors']))}</code>")
    return "\n".join(lines)


async def cmd_cancel(message: Any, state: FSMContext) -> None:
    """Выход из диалога на любом шаге."""
    if await state.get_state() is None:
        await message.answer(texts.NOTHING_TO_CANCEL)
        return
    await state.clear()
    await message.answer(texts.ADD_CANCELLED)


async def cmd_add_source(message: Any, state: FSMContext) -> None:
    """Начало диалога: выбор типа."""
    await state.clear()
    await state.set_state(AddSource.type)
    keyboard = choice_keyboard(ADD_FIELD_TYPE, TYPE_OPTIONS)
    await message.answer(texts.ADD_ASK_TYPE, reply_markup=keyboard)


async def on_type(callback: Any, callback_data: AddSourceChoice, state: FSMContext) -> None:
    """Выбран тип источника."""
    await state.update_data(type=SourceType(callback_data.value).value)
    await state.set_state(AddSource.url)
    await callback.answer()
    await callback.message.answer(texts.ADD_ASK_URL[callback_data.value])


async def on_url(message: Any, state: FSMContext, source_add_deps: SourceAddDeps) -> None:
    """Адрес источника."""
    data = await state.get_data()
    source_type = SourceType(data["type"])
    url = normalize_address(source_type, message.text or "")
    if url is None:
        bad = texts.ADD_BAD_TG if source_type is SourceType.TG else texts.ADD_BAD_URL
        await message.answer(bad)
        return
    async with source_add_deps.session_scope() as session:
        existing = await source_add_deps.source_repo(session).list_all()
    if any(source.url == url for source in existing):
        await state.clear()
        await message.answer(texts.ADD_DUPLICATE)
        return
    await state.update_data(url=url)
    await state.set_state(AddSource.name)
    await message.answer(texts.ADD_ASK_NAME)


async def on_name(message: Any, state: FSMContext, source_add_deps: SourceAddDeps) -> None:
    """Имя источника."""
    name = (message.text or "").strip()
    if not name or len(name) > NAME_MAX_CHARS:
        await message.answer(texts.ADD_BAD_NAME.format(max_len=NAME_MAX_CHARS))
        return
    await state.update_data(name=name)
    await state.set_state(AddSource.interval)
    await message.answer(texts.ADD_ASK_INTERVAL.format(min_value=source_add_deps.min_interval_min))


async def on_interval(message: Any, state: FSMContext, source_add_deps: SourceAddDeps) -> None:
    """Интервал проверки."""
    raw = (message.text or "").strip()
    minimum = source_add_deps.min_interval_min
    if not raw.isdigit() or int(raw) < minimum:
        await message.answer(texts.ADD_BAD_INTERVAL.format(min_value=minimum))
        return
    await state.update_data(interval=int(raw))
    await state.set_state(AddSource.moderation)
    await message.answer(
        texts.ADD_ASK_MODERATION,
        reply_markup=choice_keyboard(ADD_FIELD_MODERATION, MODERATION_OPTIONS),
    )


async def _ask_confirm(target: Any, state: FSMContext) -> None:
    await state.set_state(AddSource.confirm)
    await target.answer(
        _summary(await state.get_data()),
        reply_markup=choice_keyboard(ADD_FIELD_CONFIRM, CONFIRM_OPTIONS),
    )


async def on_moderation(callback: Any, callback_data: AddSourceChoice, state: FSMContext) -> None:
    """Режим модерации."""
    await state.update_data(moderation=ModerationMode(callback_data.value).value)
    await callback.answer()
    if (await state.get_data())["type"] == SourceType.WEB.value:
        await state.set_state(AddSource.selectors)
        await callback.message.answer(texts.ADD_ASK_SELECTORS)
        return
    await _ask_confirm(callback.message, state)


async def on_selectors(message: Any, state: FSMContext) -> None:
    """JSON с селекторами для сайта без RSS."""
    try:
        raw = json.loads(message.text or "")
        selectors = WebSelectors.model_validate(raw)
    except json.JSONDecodeError as exc:
        await message.answer(texts.ADD_BAD_SELECTORS.format(reason=f"не JSON ({exc.msg})"))
        return
    except ValidationError as exc:
        reason = "; ".join(str(error["msg"]) for error in exc.errors())
        await message.answer(texts.ADD_BAD_SELECTORS.format(reason=html.escape(reason)))
        return
    await state.update_data(selectors=selectors.model_dump(exclude_none=True))
    await _ask_confirm(message, state)


async def trial_collect(source: Source, deps: SourceAddDeps) -> str:
    """Пробный сбор без сохранения: первые заголовки или ошибка."""
    collector = deps.collectors.get(source.type)
    if collector is None:
        return texts.CHECK_UNSUPPORTED
    try:
        items = await collector.fetch(source)
    except Exception as exc:
        log.warning("bot.trial_collect_failed", url=source.url, error=repr(exc))
        return f"Пробный сбор: ошибка — {html.escape(str(exc))}"
    if not items:
        return texts.CHECK_EMPTY
    titles = [f"• {html.escape(item.title or item.url)}" for item in items[:TRIAL_TITLES]]
    return "\n".join([f"Пробный сбор: найдено материалов — {len(items)}", *titles])


async def on_confirm(
    callback: Any,
    callback_data: AddSourceChoice,
    state: FSMContext,
    source_add_deps: SourceAddDeps,
) -> None:
    """Пробный сбор или сохранение."""
    deps = source_add_deps
    data = await state.get_data()
    await callback.answer()
    if callback_data.value == CONFIRM_CHECK:
        trial = _source_from_state(data, TRIAL_SOURCE_ID)
        await callback.message.answer(await trial_collect(trial, deps))
        return
    try:
        async with deps.session_scope() as session:
            saved = await deps.source_repo(session).add(_source_from_state(data))
            source_id, name, source_type = saved.id, saved.name, saved.type
    except IntegrityError:
        await state.clear()
        await callback.message.answer(texts.ADD_DUPLICATE)
        return
    await state.clear()
    user_id = callback.from_user.id if callback.from_user else None
    log.info("bot.source_added", source_id=source_id, type=source_type.value, user_id=user_id)
    if source_type is SourceType.TG and deps.reload_telegram is not None:
        await deps.reload_telegram()
    saved_text = texts.ADD_SAVED.format(source_id=source_id, name=html.escape(name))
    await callback.message.answer(saved_text)


def create_router() -> Router:
    """Роутер диалога добавления источника."""
    router = Router(name="source_add")
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_add_source, Command("add_source"))
    router.callback_query.register(
        on_type, AddSource.type, AddSourceChoice.filter(F.field == ADD_FIELD_TYPE)
    )
    router.message.register(on_url, AddSource.url)
    router.message.register(on_name, AddSource.name)
    router.message.register(on_interval, AddSource.interval)
    router.callback_query.register(
        on_moderation, AddSource.moderation, AddSourceChoice.filter(F.field == ADD_FIELD_MODERATION)
    )
    router.message.register(on_selectors, AddSource.selectors)
    router.callback_query.register(
        on_confirm, AddSource.confirm, AddSourceChoice.filter(F.field == ADD_FIELD_CONFIRM)
    )
    return router
