from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage

from app.alerts import AlertService, format_alert
from app.bot import texts
from app.bot.screen import current_screen
from app.config import AlertsConfig
from app.domain.alerts import AlertEvent, AlertKind, AlertSink
from tests.config_helpers import base_section

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CFG = AlertsConfig(**base_section("alerts"))
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


@pytest.mark.parametrize("kind", [AlertKind.TG_DOWN, AlertKind.AI_STALLED])
def test_component_kinds_have_own_template(kind: AlertKind) -> None:
    """Новые виды видно и в чате, и в журнале ошибок."""
    text = format_alert(_event(kind, key=kind.value))
    assert texts.ALERT_TEMPLATES[kind.value].split("{")[0] in text
    assert kind.value in texts.ALERT_KIND_LABELS


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


class Incident:
    def __init__(self, incident_id: int, count: int) -> None:
        self.id = incident_id
        self.count = count


class FakeStore:
    """Журнал ошибок: считает записи по key и возвращает растущий count."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.events: list[AlertEvent] = []
        self._counts: dict[str, int] = {}

    async def record(self, event: AlertEvent) -> Incident:
        self.events.append(event)
        if self.error:
            raise self.error
        self._counts[event.key] = self._counts.get(event.key, 0) + 1
        return Incident(len(self._counts), self._counts[event.key])


def _muted(*kinds: AlertKind, error: Exception | None = None) -> Any:
    async def muted() -> set[AlertKind]:
        if error:
            raise error
        return set(kinds)

    return muted


async def test_event_recorded_and_hint_added() -> None:
    bot, store = FakeBot(), FakeStore()
    await AlertService(bot, ADMINS, CFG, clock=Clock(), store=store).emit(_event())
    assert [e.key for e in store.events] == ["source_down:1"]
    assert all(m["text"].endswith("\nПодробнее: /errors") for m in bot.sent)


async def test_repeat_recorded_even_when_suppressed() -> None:
    bot, clock, store = FakeBot(), Clock(), FakeStore()
    service = AlertService(bot, ADMINS, CFG, clock=clock, store=store)
    await service.emit(_event())
    await service.emit(_event())
    assert len(store.events) == 2
    assert len(bot.sent) == len(ADMINS)

    clock.now += CFG.throttle_min * 60
    await service.emit(_event())
    assert len(store.events) == 3
    assert bot.sent[-1]["text"].endswith("\nПовторов: 3 · /errors")


async def test_store_failure_does_not_block_sending() -> None:
    bot, store = FakeBot(), FakeStore(error=RuntimeError("db down"))
    await AlertService(bot, ADMINS, CFG, clock=Clock(), store=store).emit(_event())
    assert [m["chat_id"] for m in bot.sent] == ADMINS
    assert bot.sent[0]["text"].endswith("\nПодробнее: /errors")


async def test_silent_kind_recorded_but_not_sent() -> None:
    bot, clock, store = FakeBot(), Clock(), FakeStore()
    service = AlertService(
        bot,
        ADMINS,
        CFG,
        clock=clock,
        store=store,
        silent_kinds=frozenset({AlertKind.AI_ITEM_FAILED}),
    )
    silent = _event(AlertKind.AI_ITEM_FAILED, key="ai_item_failed:7")
    await service.emit(silent)
    assert [e.key for e in store.events] == ["ai_item_failed:7"]
    assert bot.sent == []

    # throttle не тронут: громкий алерт с тем же key уходит сразу
    await service.emit(_event(AlertKind.SOURCE_DOWN, key="ai_item_failed:7"))
    assert len(bot.sent) == len(ADMINS)


async def test_muted_kind_not_sent() -> None:
    bot, store = FakeBot(), FakeStore()
    service = AlertService(
        bot, ADMINS, CFG, clock=Clock(), store=store, muted=_muted(AlertKind.SOURCE_DOWN)
    )
    await service.emit(_event())
    assert len(store.events) == 1
    assert bot.sent == []

    await service.emit(_event(AlertKind.AI_DOWN, key="ai_down:x"))
    assert len(bot.sent) == len(ADMINS)


async def test_muted_failure_falls_back_to_sending() -> None:
    bot = FakeBot()
    service = AlertService(
        bot, ADMINS, CFG, clock=Clock(), muted=_muted(error=RuntimeError("db down"))
    )
    await service.emit(_event())
    assert [m["chat_id"] for m in bot.sent] == ADMINS


async def test_without_store_behaviour_unchanged() -> None:
    bot = FakeBot()
    await AlertService(bot, ADMINS, CFG, clock=Clock()).emit(_event())
    assert [m["chat_id"] for m in bot.sent] == ADMINS
    assert bot.sent[0]["text"].endswith("\nПодробнее: /errors")


def test_is_alert_sink() -> None:
    sink: AlertSink = AlertService(FakeBot(), ADMINS, CFG)
    assert sink is not None


async def test_each_admin_gets_own_screen() -> None:
    """Алерт двум админам — два экрана: сообщение одного не удалит сообщение другого."""
    screens: list[str] = []

    class ScreenBot(FakeBot):
        async def send_message(self, **kwargs: Any) -> None:
            screens.append(current_screen())
            await super().send_message(**kwargs)

    bot = ScreenBot()
    await AlertService(bot, ADMINS, CFG, clock=Clock()).emit(_event())
    assert len(screens) == len(ADMINS)
    assert len(set(screens)) == len(ADMINS)
