"""Inline-клавиатуры и CallbackData бота управления."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

BUTTONS_PER_SOURCE = 3


class SourceActionType(StrEnum):
    """Действия с источником."""

    PAGE = "page"
    TOGGLE = "toggle"
    DELETE = "delete"
    CONFIRM_DELETE = "confirm"
    CHECK = "check"


class SourceAction(CallbackData, prefix="src"):
    """Нажатие кнопки в списке источников."""

    action: SourceActionType
    id: int
    page: int


class ListedSource(Protocol):
    """Поля источника для клавиатуры (Source подходит структурно)."""

    id: int
    enabled: bool


def sources_keyboard(
    sources: Sequence[ListedSource], page: int, has_prev: bool, has_next: bool
) -> InlineKeyboardMarkup:
    """Кнопки для страницы источников: вкл/выкл, удалить, проверить и навигация."""
    builder = InlineKeyboardBuilder()
    for source in sources:
        toggle = f"⏸ #{source.id}" if source.enabled else f"▶️ #{source.id}"
        builder.button(
            text=toggle,
            callback_data=SourceAction(action=SourceActionType.TOGGLE, id=source.id, page=page),
        )
        builder.button(
            text=f"🗑 #{source.id}",
            callback_data=SourceAction(action=SourceActionType.DELETE, id=source.id, page=page),
        )
        builder.button(
            text=f"🔎 #{source.id}",
            callback_data=SourceAction(action=SourceActionType.CHECK, id=source.id, page=page),
        )
    row_sizes = [BUTTONS_PER_SOURCE] * len(sources)
    nav = 0
    if has_prev:
        builder.button(
            text="◀️", callback_data=SourceAction(action=SourceActionType.PAGE, id=0, page=page - 1)
        )
        nav += 1
    if has_next:
        builder.button(
            text="▶️", callback_data=SourceAction(action=SourceActionType.PAGE, id=0, page=page + 1)
        )
        nav += 1
    if nav:
        row_sizes.append(nav)
    if row_sizes:
        builder.adjust(*row_sizes)
    return builder.as_markup()


def confirm_delete_keyboard(source_id: int, page: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления источника."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Да, удалить",
        callback_data=SourceAction(action=SourceActionType.CONFIRM_DELETE, id=source_id, page=page),
    )
    builder.button(
        text="Отмена", callback_data=SourceAction(action=SourceActionType.PAGE, id=0, page=page)
    )
    builder.adjust(2)
    return builder.as_markup()
