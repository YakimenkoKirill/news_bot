from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.config import AiConfig
from app.domain.alerts import AlertEvent, AlertKind
from app.pipeline.ai.budget import BudgetExceeded, BudgetGuard, cost
from tests.config_helpers import base_section

CFG = AiConfig(
    **{
        **base_section("ai"),
        "price_input_per_1m": "0.256",
        "price_output_per_1m": "1.02",
        "daily_budget": "0.50",
    }
)
TZ = "Europe/Moscow"


class FakeRepo:
    def __init__(self, entries: list[tuple[datetime, Decimal]] | None = None) -> None:
        self.entries = entries or []
        self.since: list[datetime] = []

    async def cost_since(self, dt: datetime) -> Decimal:
        self.since.append(dt)
        return sum((amount for at, amount in self.entries if at >= dt), Decimal(0))


class FakeSink:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    async def emit(self, event: AlertEvent) -> None:
        self.events.append(event)


def test_cost_exact_decimal() -> None:
    assert cost(1_000_000, 0, CFG) == Decimal("0.256000")
    assert cost(0, 1_000_000, CFG) == Decimal("1.020000")
    assert cost(2_000, 300, CFG) == Decimal("0.000818")
    assert cost(0, 0, CFG) == Decimal("0")
    assert isinstance(cost(1, 1, CFG), Decimal)


def test_cost_rounds_to_column_scale() -> None:
    assert cost(1, 0, CFG) == Decimal("0.000000")
    assert cost(3, 0, CFG) == Decimal("0.000001")
    assert cost(1, 0, CFG).as_tuple().exponent == -6


async def test_under_budget_returns_spent() -> None:
    now = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    guard = BudgetGuard(FakeRepo([(now, Decimal("0.49"))]), FakeSink(), CFG, TZ)
    assert await guard.check(now) == Decimal("0.49")


async def test_exactly_budget_is_exceeded() -> None:
    now = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    sink = FakeSink()
    guard = BudgetGuard(FakeRepo([(now, Decimal("0.50"))]), sink, CFG, TZ)
    with pytest.raises(BudgetExceeded) as caught:
        await guard.check(now)
    assert caught.value.spent == Decimal("0.50")
    assert caught.value.budget == Decimal("0.50")
    [event] = sink.events
    assert event.kind is AlertKind.BUDGET_EXCEEDED
    assert event.key == "budget_exceeded:2026-10-10"


async def test_alert_once_per_day() -> None:
    now = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    sink = FakeSink()
    guard = BudgetGuard(FakeRepo([(now, Decimal("1"))]), sink, CFG, TZ)
    for _ in range(3):
        with pytest.raises(BudgetExceeded):
            await guard.check(now)
    assert len(sink.events) == 1


async def test_day_boundary_in_channel_timezone() -> None:
    # 20:30 UTC 9 октября = 23:30 по Москве, 21:30 UTC = 00:30 10 октября по Москве.
    evening = datetime(2026, 10, 9, 20, 30, tzinfo=UTC)
    after_midnight = datetime(2026, 10, 9, 21, 30, tzinfo=UTC)
    repo = FakeRepo([(evening, Decimal("0.60"))])
    sink = FakeSink()
    guard = BudgetGuard(repo, sink, CFG, TZ)

    with pytest.raises(BudgetExceeded):
        await guard.check(evening)
    assert repo.since[-1] == datetime(2026, 10, 8, 21, 0, tzinfo=UTC)

    assert await guard.check(after_midnight) == Decimal("0")
    assert repo.since[-1] == datetime(2026, 10, 9, 21, 0, tzinfo=UTC)


async def test_alert_possible_again_next_day() -> None:
    day_one = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    day_two = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    repo = FakeRepo([(day_one, Decimal("0.7")), (day_two, Decimal("0.8"))])
    sink = FakeSink()
    guard = BudgetGuard(repo, sink, CFG, TZ)
    for now in (day_one, day_one, day_two, day_two):
        with pytest.raises(BudgetExceeded):
            await guard.check(now)
    assert [e.key for e in sink.events] == [
        "budget_exceeded:2026-10-10",
        "budget_exceeded:2026-10-11",
    ]


def test_processed_post_repository_fits_protocol() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.posts import ProcessedPostRepository

    guard = BudgetGuard(ProcessedPostRepository(MagicMock()), FakeSink(), CFG, TZ)
    assert guard.day_start(datetime(2026, 10, 10, 9, 0, tzinfo=UTC)).hour == 0
