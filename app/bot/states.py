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
