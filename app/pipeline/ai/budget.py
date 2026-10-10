"""Учёт стоимости обращений к модели и дневной бюджет."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

import structlog

from app.config import AiConfig
from app.db.models import COST_SCALE
from app.domain.alerts import AlertEvent, AlertKind, AlertSink

log = structlog.get_logger(__name__)

TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)
COST_QUANT = Decimal(1).scaleb(-COST_SCALE)


class Prices(Protocol):
    """Цены за 1 млн токенов (AiConfig подходит структурно)."""

    @property
    def price_input_per_1m(self) -> Decimal:
        """Цена входных токенов."""
        raise NotImplementedError

    @property
    def price_output_per_1m(self) -> Decimal:
        """Цена выходных токенов."""
        raise NotImplementedError


def cost(prompt_tokens: int, completion_tokens: int, prices: Prices) -> Decimal:
    """Стоимость запроса в Decimal, округлённая до точности колонки processed_posts.cost."""
    total = (
        Decimal(prompt_tokens) * prices.price_input_per_1m
        + Decimal(completion_tokens) * prices.price_output_per_1m
    ) / TOKENS_PER_PRICE_UNIT
    return total.quantize(COST_QUANT, rounding=ROUND_HALF_UP)


class CostRepo(Protocol):
    """Нужная бюджету часть ProcessedPostRepository."""

    async def cost_since(self, dt: datetime) -> Decimal:
        """Суммарная стоимость начиная с dt."""
        raise NotImplementedError


class BudgetExceeded(RuntimeError):
    """Дневной бюджет на модель исчерпан."""

    def __init__(self, spent: Decimal, budget: Decimal) -> None:
        self.spent = spent
        self.budget = budget
        super().__init__(f"дневной бюджет исчерпан: потрачено {spent} из {budget}")


class BudgetGuard:
    """Останавливает обработку, когда расходы за сутки достигают daily_budget.

    Сутки считаются в часовом поясе tz (app.timezone). Алерт budget_exceeded
    отправляется не чаще одного раза за сутки.
    """

    def __init__(self, repo: CostRepo, sink: AlertSink, cfg: AiConfig, tz: str | ZoneInfo) -> None:
        self._repo = repo
        self._sink = sink
        self._budget = cfg.daily_budget
        self._tz = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
        self._alerted_day: date | None = None

    def day_start(self, now: datetime) -> datetime:
        """Начало текущих суток в часовом поясе бюджета (timezone-aware)."""
        local = now.astimezone(self._tz)
        return datetime.combine(local.date(), time.min, tzinfo=self._tz)

    async def check(self, now: datetime) -> Decimal:
        """Проверяет расходы за текущие сутки.

        Returns:
            Потраченная за сутки сумма, если бюджет не исчерпан.

        Raises:
            BudgetExceeded: Потрачено не меньше daily_budget.
        """
        start = self.day_start(now)
        spent = await self._repo.cost_since(start)
        if spent < self._budget:
            return spent
        today = start.date()
        if self._alerted_day != today:
            self._alerted_day = today
            log.warning("ai.budget_exceeded", spent=str(spent), budget=str(self._budget))
            await self._sink.emit(
                AlertEvent(
                    kind=AlertKind.BUDGET_EXCEEDED,
                    key=f"{AlertKind.BUDGET_EXCEEDED.value}:{today.isoformat()}",
                    message=(
                        f"Дневной бюджет на модель исчерпан: потрачено {spent} $ "
                        f"из {self._budget} $. Обработка остановлена до следующих суток."
                    ),
                    created_at=now,
                )
            )
        raise BudgetExceeded(spent, self._budget)
