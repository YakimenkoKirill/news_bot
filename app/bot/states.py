"""Состояния диалогов бота (FSM)."""

from aiogram.fsm.state import State, StatesGroup


class AddSource(StatesGroup):
    """Диалог добавления источника."""

    type = State()
    url = State()
    name = State()
    interval = State()
    moderation = State()
    selectors = State()
    confirm = State()


class AddKeywords(StatesGroup):
    """Диалог добавления ключевых или стоп-слов."""

    words = State()


class EditPost(StatesGroup):
    """Редактирование пересказа поста на модерации."""

    summary = State()


class EditSetting(StatesGroup):
    """Ввод нового значения параметра в /settings."""

    value = State()
