"""Команда /report: отчёт за сегодня, вчера или неделю."""

from __future__ import annotations

from typing import Any

import structlog
from aiogram import Router
from aiogram.filters import Command

from app.bot import texts
from app.bot.keyboards import ReportAction, report_keyboard
from app.reports.service import ReportDeps, ReportPeriod, build_report

log = structlog.get_logger(__name__)


async def _send(target: Any, kind: ReportPeriod, deps: ReportDeps) -> None:
    """Отправляет отчёт; клавиатура выбора периода — под последним сообщением."""
    try:
        messages = await build_report(kind, deps)
    except Exception:
        log.exception("report.failed", period=kind.value)
        await target.answer(texts.REPORT_FAILED)
        return
    keyboard = report_keyboard(kind.value)
    for index, message in enumerate(messages, 1):
        last = index == len(messages)
        await target.answer(message, reply_markup=keyboard if last else None)


async def cmd_report(message: Any, report_deps: ReportDeps) -> None:
    """Отчёт за сегодня."""
    await _send(message, ReportPeriod.TODAY, report_deps)


async def on_report_period(
    callback: Any, callback_data: ReportAction, report_deps: ReportDeps
) -> None:
    """Кнопка периода: сначала ответ Telegram, потом подсчёт (неделя считается дольше)."""
    await callback.answer(texts.REPORT_BUILDING)
    await _send(callback.message, ReportPeriod(callback_data.period), report_deps)


def create_router() -> Router:
    """Роутер команды /report."""
    router = Router(name="report")
    router.message.register(cmd_report, Command("report"))
    router.callback_query.register(on_report_period, ReportAction.filter())
    return router
