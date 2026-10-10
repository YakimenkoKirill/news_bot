"""Алерты администраторам через бота с подавлением повторов."""

from __future__ import annotations

import html
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import structlog
from aiogram.enums import ParseMode

from app.bot import texts
from app.config import AlertsConfig
from app.domain.alerts import AlertEvent

log = structlog.get_logger(__name__)

SECONDS_PER_MINUTE = 60

AdminIds = Sequence[int] | Callable[[], Awaitable[Sequence[int]]]


def format_alert(event: AlertEvent) -> str:
    """Текст алерта по шаблону для его вида (texts.ALERT_TEMPLATES)."""
    template = texts.ALERT_TEMPLATES[event.kind.value]
    return template.format(message=html.escape(event.message))


class AlertService:
    """Реализация AlertSink: отправляет алерты всем администраторам.

    Повтор с тем же key раньше throttle_min после предыдущего подавляется.
    Ошибки отправки логируются и не пробрасываются.
    """

    def __init__(
        self,
        bot: Any,
        admin_ids: AdminIds,
        cfg: AlertsConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._admin_ids = admin_ids
        self._throttle_s = cfg.throttle_min * SECONDS_PER_MINUTE
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    async def emit(self, event: AlertEvent) -> None:
        """Отправляет алерт, если такой же key не отправлялся в последние throttle_min."""
        now = self._clock()
        last = self._last_sent.get(event.key)
        if last is not None and now - last < self._throttle_s:
            log.info("alert.suppressed", key=event.key)
            return
        self._last_sent[event.key] = now
        text = format_alert(event)
        try:
            admins = await self._resolve_admins()
        except Exception:
            log.exception("alert.admins_unavailable", key=event.key)
            return
        for admin_id in admins:
            try:
                await self._bot.send_message(chat_id=admin_id, text=text, parse_mode=ParseMode.HTML)
            except Exception:
                log.exception("alert.send_failed", admin_id=admin_id, key=event.key)
        log.info("alert.sent", key=event.key, kind=event.kind.value)

    async def _resolve_admins(self) -> Sequence[int]:
        if callable(self._admin_ids):
            return await self._admin_ids()
        return self._admin_ids
