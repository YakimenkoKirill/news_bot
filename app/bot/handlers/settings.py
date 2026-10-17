"""Команда /settings: просмотр и изменение параметров без перезапуска."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.keyboards import (
    ST_EDIT,
    ST_LIST,
    ST_PRESET,
    ST_RESET,
    SettingsAction,
    setting_keyboard,
    settings_keyboard,
)
from app.bot.states import EditSetting
from app.runtime_config import KEYS_BY_NAME, RuntimeKey, RuntimeSnapshot

log = structlog.get_logger(__name__)

STATE_KEY = "setting_code"


class RuntimeEditor(Protocol):
    """Нужная боту часть RuntimeConfig."""

    @property
    def base(self) -> RuntimeSnapshot:
        """Значения из yaml без переопределений."""
        raise NotImplementedError

    async def snapshot(self) -> RuntimeSnapshot:
        """Текущие значения с переопределениями."""
        raise NotImplementedError

    async def overrides(self) -> dict[str, Any]:
        """Сохранённые переопределения."""
        raise NotImplementedError

    async def set(self, key: str, raw_value: Any, by: int) -> None:
        """Сохраняет переопределение; ValueError — значение не подошло."""
        raise NotImplementedError

    async def reset(self, key: str, by: int) -> None:
        """Убирает переопределение."""
        raise NotImplementedError


@dataclass(frozen=True)
class SettingsDeps:
    """Зависимости команды /settings (workflow_data: settings_deps).

    Attributes:
        runtime: Параметры, изменяемые без перезапуска.
        cache_s: Через сколько секунд новое значение видно всем (runtime.cache_s).
    """

    runtime: RuntimeEditor
    cache_s: float


def format_value(value: Any) -> str:
    """Человекочитаемое значение параметра для списка и подсказки."""
    if isinstance(value, list):
        return "–".join(str(item) for item in value) if value else texts.SETTINGS_EMPTY_VALUE
    return str(value)


def _spec(code: str) -> RuntimeKey | None:
    name = texts.SETTINGS_CODES.get(code)
    return KEYS_BY_NAME.get(name) if name else None


def _value(snapshot: RuntimeSnapshot, spec: RuntimeKey) -> Any:
    return getattr(getattr(snapshot, spec.section), spec.field)


async def render_settings(deps: SettingsDeps) -> tuple[str, InlineKeyboardMarkup]:
    """Список параметров: текущее значение и пометка «изменено».

    Args:
        deps: Зависимости команды.

    Returns:
        HTML-текст и кнопки параметров.
    """
    snapshot = await deps.runtime.snapshot()
    overrides = await deps.runtime.overrides()
    lines = [texts.SETTINGS_TITLE, texts.SETTINGS_HINT.format(cache_s=int(deps.cache_s)), ""]
    for code, name in texts.SETTINGS_CODES.items():
        spec = KEYS_BY_NAME[name]
        current = format_value(_value(snapshot, spec))
        default = format_value(_value(deps.runtime.base, spec)) if name in overrides else None
        lines.append(
            texts.settings_line(
                texts.SETTINGS_LABELS[code],
                html.escape(current),
                html.escape(default or "") if default is not None else None,
            )
        )
    return "\n".join(lines), settings_keyboard(list(texts.SETTINGS_CODES))


async def _show_list(message: Any, deps: SettingsDeps) -> None:
    text, keyboard = await render_settings(deps)
    await message.answer(text, reply_markup=keyboard)


async def cmd_settings(message: Any, state: FSMContext, settings_deps: SettingsDeps) -> None:
    """Показывает параметры и кнопки изменения."""
    await state.clear()
    await _show_list(message, settings_deps)


async def on_settings_action(
    callback: Any, callback_data: SettingsAction, state: FSMContext, settings_deps: SettingsDeps
) -> None:
    """Кнопки /settings: открыть параметр, применить пресет, вернуть значение из конфигурации."""
    deps = settings_deps
    if callback_data.action == ST_LIST:
        await state.clear()
        await callback.answer()
        await _show_list(callback.message, deps)
        return

    spec = _spec(callback_data.key)
    if spec is None:
        await state.clear()
        await callback.answer(texts.SETTINGS_UNKNOWN, show_alert=True)
        return

    if callback_data.action == ST_EDIT:
        await _ask_value(callback, callback_data.key, spec, state, deps)
    elif callback_data.action == ST_PRESET:
        await _apply(callback, callback_data.key, spec, callback_data.value, state, deps)
    elif callback_data.action == ST_RESET:
        await _reset(callback, callback_data.key, spec, state, deps)


async def _ask_value(
    callback: Any, code: str, spec: RuntimeKey, state: FSMContext, deps: SettingsDeps
) -> None:
    snapshot = await deps.runtime.snapshot()
    await state.set_state(EditSetting.value)
    await state.update_data({STATE_KEY: code})
    await callback.answer()
    await callback.message.answer(
        texts.SETTINGS_ASK.format(
            label=texts.SETTINGS_LABELS[code],
            value=html.escape(format_value(_value(snapshot, spec))),
            hint=html.escape(spec.hint),
        ),
        reply_markup=setting_keyboard(code, texts.SETTINGS_PRESETS.get(code, ())),
    )


async def _apply(
    callback: Any,
    code: str,
    spec: RuntimeKey,
    raw: str,
    state: FSMContext,
    deps: SettingsDeps,
) -> None:
    user_id = callback.from_user.id if callback.from_user else 0
    try:
        await deps.runtime.set(spec.name, raw, user_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    snapshot = await deps.runtime.snapshot()
    await callback.message.answer(
        texts.SETTINGS_SAVED.format(
            label=texts.SETTINGS_LABELS[code],
            value=html.escape(format_value(_value(snapshot, spec))),
        )
    )
    await _show_list(callback.message, deps)


async def _reset(
    callback: Any, code: str, spec: RuntimeKey, state: FSMContext, deps: SettingsDeps
) -> None:
    user_id = callback.from_user.id if callback.from_user else 0
    await deps.runtime.reset(spec.name, user_id)
    await state.clear()
    await callback.answer()
    await callback.message.answer(
        texts.SETTINGS_RESTORED.format(
            label=texts.SETTINGS_LABELS[code],
            value=html.escape(format_value(_value(deps.runtime.base, spec))),
        )
    )
    await _show_list(callback.message, deps)


async def on_value(message: Any, state: FSMContext, settings_deps: SettingsDeps) -> None:
    """Введённое значение параметра: сохраняем или просим ещё раз."""
    deps = settings_deps
    code = str((await state.get_data()).get(STATE_KEY, ""))
    spec = _spec(code)
    if spec is None:
        await state.clear()
        await message.answer(texts.SETTINGS_UNKNOWN)
        return
    user_id = message.from_user.id if message.from_user else 0
    try:
        await deps.runtime.set(spec.name, message.text or "", user_id)
    except ValueError as exc:
        await message.answer(texts.SETTINGS_FAILED.format(error=html.escape(str(exc))))
        return
    await state.clear()
    snapshot = await deps.runtime.snapshot()
    await message.answer(
        texts.SETTINGS_SAVED.format(
            label=texts.SETTINGS_LABELS[code],
            value=html.escape(format_value(_value(snapshot, spec))),
        )
    )
    await _show_list(message, deps)


async def cmd_cancel(message: Any, state: FSMContext) -> None:
    """Выход из ввода значения: /cancel работает и без других роутеров."""
    await state.clear()
    await message.answer(texts.ADD_CANCELLED)


def create_router() -> Router:
    """Роутер команды /settings."""
    router = Router(name="settings")
    router.message.register(cmd_settings, Command("settings"))
    router.message.register(cmd_cancel, Command("cancel"), EditSetting.value)
    router.message.register(on_value, EditSetting.value)
    actions = {ST_EDIT, ST_PRESET, ST_RESET, ST_LIST}
    router.callback_query.register(on_settings_action, SettingsAction.filter(F.action.in_(actions)))
    return router
