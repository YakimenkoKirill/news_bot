"""Проверка живости приложения для healthcheck контейнера.

Запуск: python -m app.health — печатает результат, код выхода 0 (здоров) или 1.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import HealthConfig, Settings, get_settings
from app.db.models import AiRequest, NewsItem, Setting, Source
from app.domain.enums import NewsStatus, SourceType

HEARTBEAT_SETTING = "heartbeat"
TG_ALIVE_SETTING = "tg_alive_at"
AI_OK_SETTING = "ai_ok_at"
PAUSED_SETTING = "paused"
STARTED_AT_KEY = "started_at"
BEAT_AT_KEY = "at"
SECONDS_PER_MINUTE = 60


@dataclass(frozen=True)
class HealthResult:
    """Итог проверки: здоров ли процесс и почему нет."""

    ok: bool
    reasons: list[str] = field(default_factory=list)


def heartbeat_value(started_at: datetime, beat_at: datetime | None) -> dict[str, str | None]:
    """Значение настройки heartbeat: время старта процесса и последнего сердцебиения."""
    return {
        STARTED_AT_KEY: started_at.isoformat(),
        BEAT_AT_KEY: beat_at.isoformat() if beat_at else None,
    }


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def evaluate_heartbeat(value: Any, now: datetime, cfg: HealthConfig) -> list[str]:
    """Причины нездоровья по значению heartbeat (пустой список — всё в порядке).

    Свежий heartbeat (моложе max_age_s) — здоров. Если heartbeat ещё нет или он
    старый, процесс считается здоровым startup_grace_s после своего старта.
    """
    if not isinstance(value, Mapping):
        return ["нет heartbeat: планировщик ещё ни разу не запускался"]
    beat_at = _parse(value.get(BEAT_AT_KEY))
    if beat_at is not None and now - beat_at <= timedelta(seconds=cfg.max_age_s):
        return []
    started_at = _parse(value.get(STARTED_AT_KEY))
    if started_at is not None and now - started_at <= timedelta(seconds=cfg.startup_grace_s):
        return []
    if beat_at is None:
        return [f"нет heartbeat дольше {cfg.startup_grace_s:g} с после старта"]
    age = int((now - beat_at).total_seconds())
    return [f"heartbeat устарел: {age} с (допустимо {cfg.max_age_s:g} с)"]


def evaluate_tg(value: Any, now: datetime, cfg: HealthConfig, state: HealthState) -> list[str]:
    """Причины нездоровья Telethon (пустой список — всё в порядке).

    Проверка работает только когда есть хотя бы один включённый tg-источник:
    без них Telethon не нужен. В первые startup_grace_s после старта процесса
    отметки может ещё не быть.

    Args:
        value: Значение настройки tg_alive_at.
        now: Текущий момент.
        cfg: Секция health.
        state: Собранное состояние (нужны источники и время старта).

    Returns:
        Причины для healthcheck и алерта tg_down.
    """
    if not state.has_tg_sources or _in_grace(state.started_at, now, cfg):
        return []
    alive_at = _parse(value)
    if alive_at is None:
        return [f"Telethon не на связи: отметки нет дольше {cfg.startup_grace_s:g} с после старта"]
    silence = int((now - alive_at).total_seconds())
    if silence <= cfg.tg_max_silence_s:
        return []
    return [f"Telethon не на связи: молчит {silence} с (допустимо {cfg.tg_max_silence_s:g} с)"]


def evaluate_ai(value: Any, now: datetime, cfg: HealthConfig, state: HealthState) -> list[str]:
    """Причины нездоровья обработки моделью (пустой список — всё в порядке).

    Проблема только если очередь normalized ждёт дольше ai_backlog_min и модель
    столько же молчит. Пауза пайплайна и исчерпанный дневной бюджет — не
    проблема: в этих случаях модель не должна работать.

    Args:
        value: Значение настройки ai_ok_at.
        now: Текущий момент.
        cfg: Секция health.
        state: Собранное состояние (очередь, пауза, бюджет, время старта).

    Returns:
        Причины для healthcheck и алерта ai_stalled.
    """
    if state.paused or state.budget_exhausted or not state.ai_backlog:
        return []
    if _in_grace(state.started_at, now, cfg):
        return []
    limit = timedelta(minutes=cfg.ai_backlog_min)
    ok_at = _parse(value)
    if ok_at is not None and now - ok_at <= limit:
        return []
    waiting = f"{cfg.ai_backlog_min:g} мин"
    if ok_at is None:
        return [f"модель не обрабатывает очередь: ответов нет, новости ждут дольше {waiting}"]
    silence = int((now - ok_at).total_seconds() / SECONDS_PER_MINUTE)
    return [
        f"модель не обрабатывает очередь: последний ответ {silence} мин назад, "
        f"новости ждут дольше {waiting}"
    ]


def _in_grace(started_at: datetime | None, now: datetime, cfg: HealthConfig) -> bool:
    """Идёт ли льготное время после старта процесса."""
    if started_at is None:
        return False
    return now - started_at <= timedelta(seconds=cfg.startup_grace_s)


@dataclass(frozen=True)
class HealthState:
    """Входные данные проверок, прочитанные из БД.

    Attributes:
        heartbeat: Значение настройки heartbeat.
        tg_alive_at: Значение настройки tg_alive_at.
        ai_ok_at: Значение настройки ai_ok_at.
        has_tg_sources: Есть ли включённые Telegram-источники.
        ai_backlog: Есть ли новости normalized, которые ждут дольше ai_backlog_min.
        paused: Стоит ли пайплайн на паузе.
        budget_exhausted: Исчерпан ли дневной бюджет на модель.
        started_at: Время старта процесса из heartbeat.
    """

    heartbeat: Any = None
    tg_alive_at: Any = None
    ai_ok_at: Any = None
    has_tg_sources: bool = False
    ai_backlog: bool = False
    paused: bool = False
    budget_exhausted: bool = False
    started_at: datetime | None = None


def evaluate_state(state: HealthState, now: datetime, cfg: HealthConfig) -> dict[str, list[str]]:
    """Причины нездоровья по компонентам: heartbeat, telegram и модель.

    Args:
        state: Прочитанное состояние.
        now: Текущий момент.
        cfg: Секция health.

    Returns:
        Словарь «компонент -> причины»; пустые списки означают «здоров».
    """
    return {
        "heartbeat": evaluate_heartbeat(state.heartbeat, now, cfg),
        "telegram": evaluate_tg(state.tg_alive_at, now, cfg, state),
        "ai": evaluate_ai(state.ai_ok_at, now, cfg, state),
    }


async def read_state(reader: Any, settings: Settings, now: datetime) -> HealthState:
    """Читает всё, что нужно проверкам, одним набором запросов.

    Args:
        reader: Соединение или сессия SQLAlchemy (нужен метод scalar).
        settings: Настройки приложения.
        now: Текущий момент.

    Returns:
        Состояние компонентов.
    """
    settings_values: dict[str, Any] = {}
    for key in (HEARTBEAT_SETTING, TG_ALIVE_SETTING, AI_OK_SETTING, PAUSED_SETTING):
        settings_values[key] = await reader.scalar(select(Setting.value).where(Setting.key == key))
    has_tg = await reader.scalar(
        select(exists().where(Source.enabled.is_(True), Source.type == SourceType.TG))
    )
    backlog_before = now - timedelta(minutes=settings.health.ai_backlog_min)
    backlog = await reader.scalar(
        select(
            exists().where(
                NewsItem.status == NewsStatus.NORMALIZED, NewsItem.created_at < backlog_before
            )
        )
    )
    zone = ZoneInfo(settings.app.timezone)
    day_start = datetime.combine(now.astimezone(zone).date(), time.min, tzinfo=zone)
    spent = await reader.scalar(
        select(func.coalesce(func.sum(AiRequest.cost), 0)).where(AiRequest.created_at >= day_start)
    )
    heartbeat = settings_values[HEARTBEAT_SETTING]
    started_at = _parse(heartbeat.get(STARTED_AT_KEY)) if isinstance(heartbeat, Mapping) else None
    return HealthState(
        heartbeat=heartbeat,
        tg_alive_at=settings_values[TG_ALIVE_SETTING],
        ai_ok_at=settings_values[AI_OK_SETTING],
        has_tg_sources=bool(has_tg),
        ai_backlog=bool(backlog),
        paused=bool(settings_values[PAUSED_SETTING]),
        budget_exhausted=Decimal(spent or 0) >= settings.ai.daily_budget,
        started_at=started_at,
    )


async def _read(engine: AsyncEngine, settings: Settings, now: datetime) -> HealthState:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        return await read_state(connection, settings, now)


async def check(settings: Settings, *, now: datetime | None = None) -> HealthResult:
    """Проверяет БД, heartbeat планировщика, связь Telethon и работу модели.

    Args:
        settings: Настройки приложения.
        now: Текущий момент (по умолчанию — сейчас, UTC).

    Returns:
        Результат; ошибки БД превращаются в причину без трейсбека.
    """
    cfg = settings.health
    moment = now or datetime.now(UTC)
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        state = await asyncio.wait_for(_read(engine, settings, moment), timeout=cfg.db_timeout_s)
    except TimeoutError:
        return HealthResult(ok=False, reasons=[f"БД не ответила за {cfg.db_timeout_s:g} с"])
    except (SQLAlchemyError, OSError) as exc:
        return HealthResult(ok=False, reasons=[f"БД недоступна: {type(exc).__name__}"])
    finally:
        await engine.dispose()
    reasons = [reason for group in evaluate_state(state, moment, cfg).values() for reason in group]
    return HealthResult(ok=not reasons, reasons=reasons)


def main() -> int:
    """Печатает результат проверки и возвращает код выхода."""
    result = asyncio.run(check(get_settings()))
    if result.ok:
        print("ok")
        return 0
    print("unhealthy: " + "; ".join(result.reasons))
    return 1


if __name__ == "__main__":
    sys.exit(main())
