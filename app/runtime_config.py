"""Параметры, которые меняются на лету: значения из БД поверх configs/*.yaml.

Значение из yaml остаётся значением по умолчанию, а переопределение из таблицы
settings важнее. Потребители берут снимок настроек на каждом запуске задачи,
поэтому изменение применяется без перезапуска приложения — не позже
runtime.cache_s секунд.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from app.config import AiConfig, ChannelProfile, PublisherConfig, Settings

log = structlog.get_logger(__name__)

_SectionT = TypeVar("_SectionT", bound=BaseModel)

OVERRIDES_SETTING = "runtime_overrides"
QUEUE_LIMIT_KEY = "publisher.max_queued_per_source"
TOPIC_MIN_CHARS = 3
TOPIC_MAX_CHARS = 200
QUIET_HOURS_PARTS = 2


class RuntimeSettingsRepo(Protocol):
    """Нужная настройкам часть SettingsRepository."""

    async def get(self, key: str, default: Any = None) -> Any:
        """Значение настройки."""
        raise NotImplementedError

    async def set(self, key: str, value: Any) -> None:
        """Сохраняет настройку."""
        raise NotImplementedError


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Секции конфигурации с применёнными переопределениями.

    Attributes:
        channel: Профиль канала (тема).
        ai: Параметры модели (порог оценки).
        publisher: Публикация (интервал, тихие часы, лимит источника, старение).
    """

    channel: ChannelProfile
    ai: AiConfig
    publisher: PublisherConfig


class RuntimeSource(Protocol):
    """Источник снимка изменяемых параметров (RuntimeConfig подходит структурно)."""

    async def snapshot(self) -> RuntimeSnapshot:
        """Секции конфигурации с применёнными переопределениями."""
        raise NotImplementedError


def _parse_topic(raw: Any) -> str:
    topic = str(raw).strip()
    if not TOPIC_MIN_CHARS <= len(topic) <= TOPIC_MAX_CHARS:
        raise ValueError(
            f"тема канала: нужно от {TOPIC_MIN_CHARS} до {TOPIC_MAX_CHARS} символов, "
            f"получено {len(topic)}"
        )
    return topic


def _parse_float(raw: Any) -> float:
    try:
        return float(str(raw).strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"нужно число, получено {raw!r}") from exc


def _parse_int(raw: Any) -> int:
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"нужно целое число, получено {raw!r}") from exc


def _parse_quiet_hours(raw: Any) -> list[str]:
    """Тихие часы: «23:00-07:00», «23:00,07:00», список или пусто.

    Args:
        raw: Значение от администратора или из БД.

    Returns:
        Пустой список (тихих часов нет) или две строки ЧЧ:ММ.

    Raises:
        ValueError: Границ не две.
    """
    if isinstance(raw, list):
        parts = [str(item).strip() for item in raw if str(item).strip()]
    else:
        text = str(raw).strip().lower()
        if text in {"", "-", "нет", "none", "[]"}:
            parts = []
        else:
            parts = [part.strip() for part in text.replace("-", ",").split(",") if part.strip()]
    if parts and len(parts) != QUIET_HOURS_PARTS:
        raise ValueError("тихие часы: нужно две границы ЧЧ:ММ (например «23:00-07:00») или пусто")
    return parts


@dataclass(frozen=True)
class RuntimeKey:
    """Изменяемый на лету параметр.

    Attributes:
        name: Полное имя: «секция.ключ».
        section: Секция конфигурации (channel, ai, publisher).
        field: Поле секции.
        parse: Приведение значения к типу поля; бросает ValueError.
        hint: Подсказка администратору о допустимых значениях.
    """

    name: str
    section: str
    field: str
    parse: Callable[[Any], Any]
    hint: str


RUNTIME_KEYS: tuple[RuntimeKey, ...] = (
    RuntimeKey(
        name="channel.topic",
        section="channel",
        field="topic",
        parse=_parse_topic,
        hint=f"текст от {TOPIC_MIN_CHARS} до {TOPIC_MAX_CHARS} символов",
    ),
    RuntimeKey(
        name="ai.min_score",
        section="ai",
        field="min_score",
        parse=_parse_float,
        hint="число от 0 до 1",
    ),
    RuntimeKey(
        name="publisher.min_interval_min",
        section="publisher",
        field="min_interval_min",
        parse=_parse_float,
        hint="минуты, не меньше 1",
    ),
    RuntimeKey(
        name="publisher.quiet_hours",
        section="publisher",
        field="quiet_hours",
        parse=_parse_quiet_hours,
        hint="«23:00-07:00» или пусто",
    ),
    RuntimeKey(
        name="publisher.max_queued_per_source",
        section="publisher",
        field="max_queued_per_source",
        parse=_parse_int,
        hint="целое число, 0 — без лимита",
    ),
    RuntimeKey(
        name="publisher.score_decay_per_h",
        section="publisher",
        field="score_decay_per_h",
        parse=_parse_float,
        hint="неотрицательное число",
    ),
)
KEYS_BY_NAME = {key.name: key for key in RUNTIME_KEYS}
MIN_INTERVAL_MIN = 1.0


def _check_extra(key: RuntimeKey, value: Any) -> None:
    """Проверки, которых нет в pydantic-модели секции.

    Args:
        key: Параметр.
        value: Приведённое значение.

    Raises:
        ValueError: Значение вне допустимого диапазона.
    """
    if key.name == "publisher.min_interval_min" and float(value) < MIN_INTERVAL_MIN:
        raise ValueError(f"интервал между постами: не меньше {MIN_INTERVAL_MIN:.0f} минуты")


def validate_value(key: RuntimeKey, raw: Any, base: BaseModel) -> Any:
    """Приводит значение к типу поля и проверяет его моделью секции.

    Args:
        key: Параметр.
        raw: Значение от администратора или из БД.
        base: Текущая секция конфигурации (для остальных полей).

    Returns:
        Проверенное значение.

    Raises:
        ValueError: Значение неверного типа или не прошло проверку секции.
    """
    value = key.parse(raw)
    _check_extra(key, value)
    try:
        type(base).model_validate({**base.model_dump(), key.field: value})
    except ValidationError as exc:
        problems = "; ".join(error["msg"] for error in exc.errors())
        raise ValueError(f"{key.name}: {problems} ({key.hint})") from exc
    return value


class RuntimeConfig:
    """Секции конфигурации с переопределениями из БД.

    Переопределения лежат одним словарём в настройке runtime_overrides, поэтому
    чтение — один запрос. Снимок кэшируется на runtime.cache_s секунд: задачи
    вызывают snapshot на каждом запуске, но в БД это не превращается в поток
    запросов. После set и reset кэш сбрасывается сразу.
    """

    def __init__(
        self,
        session_scope: Callable[[], AbstractAsyncContextManager[Any]],
        settings_repo: Callable[[Any], RuntimeSettingsRepo],
        settings: Settings,
        *,
        clock: Callable[[], float] = monotonic,
        on_change: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._scope = session_scope
        self._settings_repo = settings_repo
        self._base = RuntimeSnapshot(
            channel=settings.channel, ai=settings.ai, publisher=settings.publisher
        )
        self._cache_s = settings.runtime.cache_s
        self._clock = clock
        self._on_change = on_change
        self._snapshot: RuntimeSnapshot | None = None
        self._read_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def base(self) -> RuntimeSnapshot:
        """Значения из yaml без переопределений."""
        return self._base

    async def snapshot(self) -> RuntimeSnapshot:
        """Текущие секции с переопределениями.

        Returns:
            Снимок из кэша или свежий, если кэш старше runtime.cache_s.
        """
        async with self._lock:
            now = self._clock()
            if self._snapshot is not None and now - self._read_at < self._cache_s:
                return self._snapshot
            overrides = await self._load()
            self._snapshot = self._apply(overrides)
            self._read_at = now
            return self._snapshot

    async def overrides(self) -> dict[str, Any]:
        """Сохранённые переопределения (ключ -> значение)."""
        return await self._load()

    async def set(self, key: str, raw_value: Any, by: int) -> None:
        """Сохраняет переопределение параметра.

        Args:
            key: Имя параметра из RUNTIME_KEYS.
            raw_value: Новое значение (строка от администратора подойдёт).
            by: Telegram ID администратора — для лога.

        Raises:
            ValueError: Неизвестный ключ или неверное значение; в БД при этом
                ничего не записывается.
        """
        spec = self._key(key)
        section = getattr(self._base, spec.section)
        value = validate_value(spec, raw_value, section)
        async with self._lock:
            overrides = await self._load()
            previous = overrides.get(key, getattr(section, spec.field))
            overrides[key] = value
            await self._save(overrides)
            self._invalidate()
        log.info("runtime.set", key=key, old=repr(previous), new=repr(value), admin_id=by)
        await self._notify(key)

    async def reset(self, key: str, by: int) -> None:
        """Убирает переопределение: параметр снова берётся из yaml.

        Args:
            key: Имя параметра из RUNTIME_KEYS.
            by: Telegram ID администратора — для лога.

        Raises:
            ValueError: Неизвестный ключ.
        """
        spec = self._key(key)
        async with self._lock:
            overrides = await self._load()
            previous = overrides.pop(key, None)
            if previous is not None:
                await self._save(overrides)
            self._invalidate()
        default = getattr(getattr(self._base, spec.section), spec.field)
        log.info("runtime.reset", key=key, old=repr(previous), new=repr(default), admin_id=by)
        await self._notify(key)

    def _key(self, key: str) -> RuntimeKey:
        spec = KEYS_BY_NAME.get(key)
        if spec is None:
            available = ", ".join(KEYS_BY_NAME)
            raise ValueError(f"неизвестный параметр {key!r}; доступны: {available}")
        return spec

    def _invalidate(self) -> None:
        self._snapshot = None
        self._read_at = 0.0

    async def _notify(self, key: str) -> None:
        if self._on_change is None:
            return
        await self._on_change(key)

    async def _load(self) -> dict[str, Any]:
        async with self._scope() as session:
            stored = await self._settings_repo(session).get(OVERRIDES_SETTING, {})
        if not isinstance(stored, Mapping):
            log.error("runtime.bad_overrides", stored=repr(stored))
            return {}
        return dict(stored)

    async def _save(self, overrides: Mapping[str, Any]) -> None:
        async with self._scope() as session:
            await self._settings_repo(session).set(OVERRIDES_SETTING, dict(overrides))

    def _apply(self, overrides: Mapping[str, Any]) -> RuntimeSnapshot:
        """Накладывает переопределения на секции из yaml.

        Повреждённое значение (например, правка в БД руками) логируется и
        пропускается: параметр берётся из yaml, приложение продолжает работу.
        """
        updates: dict[str, dict[str, Any]] = {}
        for key, raw in overrides.items():
            spec = KEYS_BY_NAME.get(key)
            if spec is None:
                log.error("runtime.unknown_override", key=key)
                continue
            section = getattr(self._base, spec.section)
            try:
                value = validate_value(spec, raw, section)
            except ValueError as exc:
                log.error("runtime.bad_override", key=key, value=repr(raw), error=str(exc))
                continue
            updates.setdefault(spec.section, {})[spec.field] = value
        return RuntimeSnapshot(
            channel=_updated(self._base.channel, updates.get("channel")),
            ai=_updated(self._base.ai, updates.get("ai")),
            publisher=_updated(self._base.publisher, updates.get("publisher")),
        )


def _updated(section: _SectionT, values: Mapping[str, Any] | None) -> _SectionT:
    """Секция с новыми значениями, проверенная заново."""
    if not values:
        return section
    return type(section).model_validate({**section.model_dump(), **values})


def key_names() -> Sequence[str]:
    """Имена изменяемых параметров в порядке объявления."""
    return [key.name for key in RUNTIME_KEYS]
