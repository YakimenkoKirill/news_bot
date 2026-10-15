"""Алерты администраторам через бота с подавлением повторов."""

from __future__ import annotations

import html
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import structlog
from aiogram.enums import ParseMode

from app.bot import texts
from app.bot.screen import new_screen
from app.config import AlertsConfig
from app.domain.alerts import AlertEvent, AlertKind

log = structlog.get_logger(__name__)

SECONDS_PER_MINUTE = 60
MORE_HINT = "Подробнее: /errors"
REPEATS_HINT = "Повторов: {count} · /errors"

AdminIds = Sequence[int] | Callable[[], Awaitable[Sequence[int]]]


class RecordedIncident(Protocol):
    """Запись журнала (Incident подходит структурно)."""

    id: int
    count: int


class IncidentStore(Protocol):
    """Журнал ошибок (поверх IncidentRepository)."""

    async def record(self, event: AlertEvent) -> RecordedIncident | None:
        """Сохраняет событие и возвращает инцидент."""
        raise NotImplementedError


class MutedKinds(Protocol):
    """Виды алертов, которые не нужно присылать в чат."""

    async def __call__(self) -> set[AlertKind]:
        """Заглушённые виды."""
        raise NotImplementedError


def format_alert(event: AlertEvent) -> str:
    """Текст алерта по шаблону для его вида (texts.ALERT_TEMPLATES)."""
    template = texts.ALERT_TEMPLATES[event.kind.value]
    return template.format(message=html.escape(event.message))


class AlertService:
    """Реализация AlertSink: отправляет алерты всем администраторам.

    Каждое событие сначала пишется в журнал (store), в том числе повтор, который
    потом подавит throttle, и тихий вид. Повтор с тем же key раньше throttle_min
    после предыдущего в чат не отправляется. Ошибки журнала и отправки логируются
    и не пробрасываются: если упала БД, администраторы всё равно получат алерт.
    """

    def __init__(
        self,
        bot: Any,
        admin_ids: AdminIds,
        cfg: AlertsConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        store: IncidentStore | None = None,
        muted: MutedKinds | None = None,
        silent_kinds: frozenset[AlertKind] = frozenset(),
    ) -> None:
        self._bot = bot
        self._admin_ids = admin_ids
        self._throttle_s = cfg.throttle_min * SECONDS_PER_MINUTE
        self._clock = clock
        self._store = store
        self._muted = muted
        self._silent_kinds = silent_kinds
        self._last_sent: dict[str, float] = {}

    async def emit(self, event: AlertEvent) -> None:
        """Пишет событие в журнал и отправляет алерт, если вид не заглушён и не подавлен."""
        incident = await self._record(event)
        if await self._is_silent(event.kind):
            log.info("alert.silent", key=event.key, kind=event.kind.value)
            return
        now = self._clock()
        last = self._last_sent.get(event.key)
        if last is not None and now - last < self._throttle_s:
            log.info("alert.suppressed", key=event.key)
            return
        self._last_sent[event.key] = now
        count = incident.count if incident is not None else 1
        hint = REPEATS_HINT.format(count=count) if count > 1 else MORE_HINT
        text = f"{format_alert(event)}\n{hint}"
        try:
            admins = await self._resolve_admins()
        except Exception:
            log.exception("alert.admins_unavailable", key=event.key)
            return
        for admin_id in admins:
            try:
                with new_screen():
                    await self._bot.send_message(
                        chat_id=admin_id, text=text, parse_mode=ParseMode.HTML
                    )
            except Exception:
                log.exception("alert.send_failed", admin_id=admin_id, key=event.key)
        log.info("alert.sent", key=event.key, kind=event.kind.value)

    async def _record(self, event: AlertEvent) -> RecordedIncident | None:
        if self._store is None:
            return None
        try:
            return await self._store.record(event)
        except Exception:
            log.exception("alert.store_failed", key=event.key)
            return None

    async def _is_silent(self, kind: AlertKind) -> bool:
        if kind in self._silent_kinds:
            return True
        if self._muted is None:
            return False
        try:
            return kind in await self._muted()
        except Exception:
            log.exception("alert.muted_unavailable", kind=kind.value)
            return False

    async def _resolve_admins(self) -> Sequence[int]:
        if callable(self._admin_ids):
            return await self._admin_ids()
        return self._admin_ids
