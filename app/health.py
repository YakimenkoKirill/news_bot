"""Проверка живости приложения для healthcheck контейнера.

Запуск: python -m app.health — печатает результат, код выхода 0 (здоров) или 1.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import HealthConfig, Settings, get_settings
from app.db.models import Setting

HEARTBEAT_SETTING = "heartbeat"
STARTED_AT_KEY = "started_at"
BEAT_AT_KEY = "at"


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


async def _read_heartbeat(engine: AsyncEngine) -> Any:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        query = select(Setting.value).where(Setting.key == HEARTBEAT_SETTING)
        return await connection.scalar(query)


async def check(settings: Settings, *, now: datetime | None = None) -> HealthResult:
    """Проверяет, что БД отвечает за db_timeout_s и heartbeat свежий.

    Args:
        settings: Настройки приложения.
        now: Текущий момент (по умолчанию — сейчас, UTC).

    Returns:
        Результат; ошибки БД превращаются в причину без трейсбека.
    """
    cfg = settings.health
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        value = await asyncio.wait_for(_read_heartbeat(engine), timeout=cfg.db_timeout_s)
    except TimeoutError:
        return HealthResult(ok=False, reasons=[f"БД не ответила за {cfg.db_timeout_s:g} с"])
    except (SQLAlchemyError, OSError) as exc:
        return HealthResult(ok=False, reasons=[f"БД недоступна: {type(exc).__name__}"])
    finally:
        await engine.dispose()
    reasons = evaluate_heartbeat(value, now or datetime.now(UTC), cfg)
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
