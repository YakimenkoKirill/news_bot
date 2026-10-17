"""Ежедневная отправка отчёта администраторам."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import structlog
from aiogram.enums import ParseMode

from app.bot.keyboards import report_keyboard
from app.bot.screen import new_screen
from app.config import DailyReportConfig, ReportDay
from app.reports.service import ReportDeps, ReportPeriod, build_report

log = structlog.get_logger(__name__)

DAILY_REPORT_SETTING = "daily_report_last"

REPORT_PERIODS: dict[ReportDay, ReportPeriod] = {
    "previous": ReportPeriod.YESTERDAY,
    "current": ReportPeriod.TODAY,
}

AdminIds = Sequence[int] | Callable[[], Awaitable[Sequence[int]]]


class LockingSettings(Protocol):
    """Нужная отправителю часть SettingsRepository."""

    async def get_for_update(self, key: str, default: Any = None) -> Any:
        """Значение настройки с блокировкой строки."""
        raise NotImplementedError

    async def set(self, key: str, value: Any) -> None:
        """Сохраняет настройку."""
        raise NotImplementedError


class DailyReportSender:
    """Отправляет отчёт за сутки всем администраторам один раз.

    Отчётный день — вчерашний (daily_report.report_day = previous) или сегодняшний
    (current) в часовом поясе канала. Дата отчётного дня читается и пишется в одной
    транзакции с блокировкой строки настройки, поэтому два экземпляра приложения
    пришлют отчёт один раз, а перезапуск после отправки не повторит её.
    """

    def __init__(
        self,
        bot: Any,
        admin_ids: AdminIds,
        settings_repo: Callable[[Any], LockingSettings],
        deps: ReportDeps,
        cfg: DailyReportConfig,
    ) -> None:
        self._bot = bot
        self._admin_ids = admin_ids
        self._settings_repo = settings_repo
        self._deps = deps
        self._cfg = cfg

    async def send_due(self, now: datetime) -> bool:
        """Отправляет отчёт, если пришло время и за этот день он ещё не уходил.

        Args:
            now: Текущий момент.

        Returns:
            True, если отчёт отправлен хотя бы одному администратору.
        """
        if not self._cfg.enabled:
            return False
        local = now.astimezone(self._deps.tz)
        if local.timetz().replace(tzinfo=None) < self._cfg.send_time:
            return False

        period = REPORT_PERIODS[self._cfg.report_day]
        day = self._report_day(local.date()).isoformat()
        async with self._deps.session_scope() as session:
            settings = self._settings_repo(session)
            if await settings.get_for_update(DAILY_REPORT_SETTING) == day:
                return False
            sent = await self._send_to_admins(period)
            if not sent:
                return False
            await settings.set(DAILY_REPORT_SETTING, day)
        log.info("report.daily_sent", day=day, period=period.value)
        return True

    def _report_day(self, today: date) -> date:
        if self._cfg.report_day == "previous":
            return today - timedelta(days=1)
        return today

    async def _send_to_admins(self, period: ReportPeriod) -> bool:
        messages = await build_report(period, self._deps)
        keyboard = report_keyboard(period.value)
        delivered = False
        for admin_id in await self._resolve_admins():
            try:
                with new_screen():
                    for index, text in enumerate(messages, 1):
                        await self._bot.send_message(
                            chat_id=admin_id,
                            text=text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=keyboard if index == len(messages) else None,
                        )
            except Exception:
                log.exception("report.send_failed", admin_id=admin_id)
                continue
            delivered = True
        return delivered

    async def _resolve_admins(self) -> Sequence[int]:
        if callable(self._admin_ids):
            return await self._admin_ids()
        return self._admin_ids
