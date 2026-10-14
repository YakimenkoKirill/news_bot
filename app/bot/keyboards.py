"""Inline-клавиатуры и CallbackData бота управления."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.domain.enums import NewsStatus

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


class AddSourceChoice(CallbackData, prefix="add"):
    """Нажатие кнопки в диалоге добавления источника."""

    field: str
    value: str


ADD_FIELD_TYPE = "type"
ADD_FIELD_MODERATION = "moderation"
ADD_FIELD_CONFIRM = "confirm"
CONFIRM_CHECK = "check"
CONFIRM_SAVE = "save"


def choice_keyboard(field: str, options: Sequence[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Кнопки выбора: (значение, подпись)."""
    builder = InlineKeyboardBuilder()
    for value, label in options:
        builder.button(text=label, callback_data=AddSourceChoice(field=field, value=value))
    builder.adjust(len(options))
    return builder.as_markup()


class KeywordAction(CallbackData, prefix="kw"):
    """Кнопки управления словами: удалить, добавить, выбрать тип."""

    action: str
    id: int = 0
    value: str = ""


KW_DELETE = "delete"
KW_ADD = "add"
KW_TYPE = "type"


class ReviewActionType(StrEnum):
    """Решение модератора."""

    PUBLISH = "publish"
    EDIT = "edit"
    REJECT = "reject"
    LATER = "later"


class ReviewAction(CallbackData, prefix="rv"):
    """Кнопка под постом на модерации."""

    action: ReviewActionType
    post_id: int


REVIEW_BUTTONS = [
    (ReviewActionType.PUBLISH, "✅ Опубликовать"),
    (ReviewActionType.EDIT, "✏️ Изменить"),
    (ReviewActionType.REJECT, "🚫 Отклонить"),
    (ReviewActionType.LATER, "⏰ Позже"),
]
REVIEW_BUTTONS_PER_ROW = 2


def review_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Кнопки модерации поста."""
    builder = InlineKeyboardBuilder()
    for action, label in REVIEW_BUTTONS:
        builder.button(text=label, callback_data=ReviewAction(action=action, post_id=post_id))
    builder.adjust(REVIEW_BUTTONS_PER_ROW)
    return builder.as_markup()


class QueueTab(StrEnum):
    """Вкладка списка /queue."""

    QUEUE = "queue"
    REVIEW = "review"


QUEUE_TAB_STATUSES: dict[QueueTab, tuple[NewsStatus, ...]] = {
    QueueTab.QUEUE: (NewsStatus.APPROVED, NewsStatus.SCHEDULED),
    QueueTab.REVIEW: (NewsStatus.PENDING_REVIEW,),
}
QUEUE_TAB_LABELS = {QueueTab.QUEUE: "В очереди", QueueTab.REVIEW: "На модерации"}
QUEUE_ACTIVE_MARK = "• "
QUEUE_OPEN_PER_ROW = 5


class QueueActionType(StrEnum):
    """Действия в списке /queue."""

    PAGE = "page"
    OPEN = "open"
    BACK = "back"
    PUBLISH = "publish"
    CONFIRM = "confirm"
    CANCEL = "cancel"


class QueueAction(CallbackData, prefix="q"):
    """Нажатие кнопки в списке /queue или под предпросмотром."""

    action: QueueActionType
    tab: QueueTab
    page: int
    news_id: int = 0


def queue_keyboard(
    positions: Sequence[tuple[int, int]],
    tab: QueueTab,
    page: int,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    """Кнопки страницы /queue: «👁 N» на каждый пост, вкладки и навигация.

    Args:
        positions: Пары (номер в очереди, id новости).
        tab: Текущая вкладка.
        page: Текущая страница.
        has_prev: Есть предыдущая страница.
        has_next: Есть следующая страница.
    """
    builder = InlineKeyboardBuilder()
    for number, news_id in positions:
        builder.button(
            text=f"👁 {number}",
            callback_data=QueueAction(
                action=QueueActionType.OPEN, tab=tab, page=page, news_id=news_id
            ),
        )
    full_rows, rest = divmod(len(positions), QUEUE_OPEN_PER_ROW)
    row_sizes = [QUEUE_OPEN_PER_ROW] * full_rows + ([rest] if rest else [])
    for item in QueueTab:
        mark = QUEUE_ACTIVE_MARK if item is tab else ""
        builder.button(
            text=f"{mark}{QUEUE_TAB_LABELS[item]}",
            callback_data=QueueAction(action=QueueActionType.PAGE, tab=item, page=0),
        )
    row_sizes.append(len(QueueTab))
    nav = 0
    if has_prev:
        builder.button(
            text="◀️",
            callback_data=QueueAction(action=QueueActionType.PAGE, tab=tab, page=page - 1),
        )
        nav += 1
    if has_next:
        builder.button(
            text="▶️",
            callback_data=QueueAction(action=QueueActionType.PAGE, tab=tab, page=page + 1),
        )
        nav += 1
    if nav:
        row_sizes.append(nav)
    builder.adjust(*row_sizes)
    return builder.as_markup()


def queue_item_keyboard(news_id: int, tab: QueueTab, page: int) -> InlineKeyboardMarkup:
    """Кнопки под предпросмотром поста из /queue: «Опубликовать сейчас» и «К списку»."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚀 Опубликовать сейчас",
        callback_data=QueueAction(
            action=QueueActionType.PUBLISH, tab=tab, page=page, news_id=news_id
        ),
    )
    builder.button(
        text="◀️ К списку",
        callback_data=QueueAction(action=QueueActionType.BACK, tab=tab, page=page, news_id=news_id),
    )
    builder.adjust(1)
    return builder.as_markup()


def queue_confirm_keyboard(news_id: int, tab: QueueTab, page: int) -> InlineKeyboardMarkup:
    """Подтверждение публикации поста вне очереди."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Да, опубликовать",
        callback_data=QueueAction(
            action=QueueActionType.CONFIRM, tab=tab, page=page, news_id=news_id
        ),
    )
    builder.button(
        text="Отмена",
        callback_data=QueueAction(
            action=QueueActionType.CANCEL, tab=tab, page=page, news_id=news_id
        ),
    )
    builder.adjust(2)
    return builder.as_markup()


class ErrorTab(StrEnum):
    """Вкладка журнала ошибок /errors."""

    OPEN = "open"
    ALL = "all"
    NEWS = "news"


ERROR_TAB_LABELS = {
    ErrorTab.OPEN: "Открытые",
    ErrorTab.ALL: "Все",
    ErrorTab.NEWS: "Новости",
}
ERROR_OPEN_PER_ROW = 5


class ErrorActionType(StrEnum):
    """Действия в журнале ошибок."""

    PAGE = "page"
    OPEN = "open"
    BACK = "back"
    RESOLVE = "resolve"
    RESOLVE_ALL = "resolve_all"
    CONFIRM_ALL = "confirm_all"
    MUTE = "mute"
    RETRY = "retry"


class ErrorAction(CallbackData, prefix="er"):
    """Нажатие кнопки в журнале ошибок; id — инцидент или новость."""

    action: ErrorActionType
    tab: ErrorTab
    page: int
    id: int = 0


def errors_keyboard(
    numbered_ids: Sequence[tuple[int, int]],
    tab: ErrorTab,
    page: int,
    has_prev: bool,
    has_next: bool,
    can_resolve_all: bool,
) -> InlineKeyboardMarkup:
    """Кнопки страницы /errors: «🔎 N» на запись, вкладки, навигация и «Решить все».

    Args:
        numbered_ids: Пары (номер в списке, id записи).
        tab: Текущая вкладка.
        page: Текущая страница.
        has_prev: Есть предыдущая страница.
        has_next: Есть следующая страница.
        can_resolve_all: Показывать ли кнопку «Решить все».
    """
    builder = InlineKeyboardBuilder()
    for number, item_id in numbered_ids:
        builder.button(
            text=f"🔎 {number}",
            callback_data=ErrorAction(action=ErrorActionType.OPEN, tab=tab, page=page, id=item_id),
        )
    full_rows, rest = divmod(len(numbered_ids), ERROR_OPEN_PER_ROW)
    row_sizes = [ERROR_OPEN_PER_ROW] * full_rows + ([rest] if rest else [])
    for item in ErrorTab:
        mark = QUEUE_ACTIVE_MARK if item is tab else ""
        builder.button(
            text=f"{mark}{ERROR_TAB_LABELS[item]}",
            callback_data=ErrorAction(action=ErrorActionType.PAGE, tab=item, page=0),
        )
    row_sizes.append(len(ErrorTab))
    if can_resolve_all:
        builder.button(
            text="✅ Решить все",
            callback_data=ErrorAction(action=ErrorActionType.RESOLVE_ALL, tab=tab, page=page),
        )
        row_sizes.append(1)
    nav = 0
    if has_prev:
        builder.button(
            text="◀️",
            callback_data=ErrorAction(action=ErrorActionType.PAGE, tab=tab, page=page - 1),
        )
        nav += 1
    if has_next:
        builder.button(
            text="▶️",
            callback_data=ErrorAction(action=ErrorActionType.PAGE, tab=tab, page=page + 1),
        )
        nav += 1
    if nav:
        row_sizes.append(nav)
    builder.adjust(*row_sizes)
    return builder.as_markup()


def incident_keyboard(
    incident_id: int, kind_muted: bool, is_open: bool, tab: ErrorTab, page: int
) -> InlineKeyboardMarkup:
    """Кнопки карточки инцидента: «Решено», переключение чата и возврат к списку."""
    builder = InlineKeyboardBuilder()
    if is_open:
        builder.button(
            text="✅ Решено",
            callback_data=ErrorAction(
                action=ErrorActionType.RESOLVE, tab=tab, page=page, id=incident_id
            ),
        )
    builder.button(
        text="🔔 Присылать в чат" if kind_muted else "🔕 Не присылать в чат",
        callback_data=ErrorAction(action=ErrorActionType.MUTE, tab=tab, page=page, id=incident_id),
    )
    builder.button(
        text="◀️ К списку",
        callback_data=ErrorAction(action=ErrorActionType.BACK, tab=tab, page=page),
    )
    builder.adjust(1)
    return builder.as_markup()


def failed_news_keyboard(news_id: int, tab: ErrorTab, page: int) -> InlineKeyboardMarkup:
    """Кнопки карточки упавшей новости."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔁 Повторить",
        callback_data=ErrorAction(action=ErrorActionType.RETRY, tab=tab, page=page, id=news_id),
    )
    builder.button(
        text="◀️ К списку",
        callback_data=ErrorAction(action=ErrorActionType.BACK, tab=tab, page=page),
    )
    builder.adjust(1)
    return builder.as_markup()


def confirm_resolve_all_keyboard(tab: ErrorTab, page: int) -> InlineKeyboardMarkup:
    """Подтверждение закрытия всех открытых инцидентов."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Да, закрыть все",
        callback_data=ErrorAction(action=ErrorActionType.CONFIRM_ALL, tab=tab, page=page),
    )
    builder.button(
        text="Отмена",
        callback_data=ErrorAction(action=ErrorActionType.BACK, tab=tab, page=page),
    )
    builder.adjust(2)
    return builder.as_markup()
