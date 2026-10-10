from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage

from app.alerts import AlertService, format_alert
from app.bot import texts
from app.config import AlertsConfig
from app.domain.alerts import AlertEvent, AlertKind, AlertSink

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CFG = AlertsConfig(throttle_min=30)
ADMINS = [111, 222]
METHOD = SendMessage(chat_id=1, text="x")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeBot:
    def __init__(self, failing: dict[int, Exception] | None = None) -> None:
        self.failing = failing or {}
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        if kwargs["chat_id"] in self.failing:
            raise self.failing[kwargs["chat_id"]]
        self.sent.append(kwargs)


def _event(kind: AlertKind = AlertKind.SOURCE_DOWN, key: str = "source_down:1") -> AlertEvent:
    return AlertEvent(kind=kind, key=key, message="Источник «A & B» <упал>", created_at=NOW)


@pytest.mark.parametrize("kind", list(AlertKind))
def test_format_each_kind(kind: AlertKind) -> None:
    text = format_alert(_event(kind))
    assert text.startswith(texts.ALERT_TEMPLATES[kind.value].split("{")[0])
    assert "Источник «A &amp; B» &lt;упал&gt;" in text


async def test_sent_to_all_admins_as_html() -> None:
    bot = FakeBot()
    await AlertService(bot, ADMINS, CFG, clock=Clock()).emit(_event())
    assert [m["chat_id"] for m in bot.sent] == ADMINS
    assert all(m["parse_mode"] == "HTML" for m in bot.sent)


async def test_repeat_suppressed_and_sent_again_after_throttle() -> None:
    bot, clock = FakeBot(), Clock()
    service = AlertService(bot, ADMINS, CFG, clock=clock)
    await service.emit(_event())
    clock.now += CFG.throttle_min * 60 - 1
    await service.emit(_event())
    assert len(bot.sent) == len(ADMINS)
    clock.now += 1
    await service.emit(_event())
    assert len(bot.sent) == 2 * len(ADMINS)


async def test_different_keys_not_suppressed() -> None:
    bot = FakeBot()
    service = AlertService(bot, ADMINS, CFG, clock=Clock())
    await service.emit(_event(key="source_down:1"))
    await service.emit(_event(key="source_down:2"))
    assert len(bot.sent) == 2 * len(ADMINS)


async def test_send_errors_logged_not_raised() -> None:
    bot = FakeBot(
        failing={
            111: TelegramForbiddenError(method=METHOD, message="bot was blocked"),
            222: TelegramNetworkError(method=METHOD, message="network down"),
        }
    )
    await AlertService(bot, ADMINS, CFG, clock=Clock()).emit(_event())
    assert bot.sent == []


async def test_one_admin_error_does_not_block_others() -> None:
    bot = FakeBot(failing={111: TelegramForbiddenError(method=METHOD, message="blocked")})
    await AlertService(bot, ADMINS, CFG, clock=Clock()).emit(_event())
    assert [m["chat_id"] for m in bot.sent] == [222]


async def test_admin_ids_from_async_source() -> None:
    async def admins() -> Sequence[int]:
        return [7, 8]

    bot = FakeBot()
    await AlertService(bot, admins, CFG, clock=Clock()).emit(_event())
    assert [m["chat_id"] for m in bot.sent] == [7, 8]


async def test_admin_source_failure_does_not_raise() -> None:
    async def broken() -> Sequence[int]:
        raise RuntimeError("db down")

    bot = FakeBot()
    await AlertService(bot, broken, CFG, clock=Clock()).emit(_event())
    assert bot.sent == []


def test_is_alert_sink() -> None:
    sink: AlertSink = AlertService(FakeBot(), ADMINS, CFG)
    assert sink is not None
