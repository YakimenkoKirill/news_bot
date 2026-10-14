from datetime import datetime
from typing import Any

import pytest
import structlog
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)
from aiogram.types import InlineKeyboardMarkup, Update

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import queue as queue_handlers
from app.bot.keyboards import (
    QueueAction,
    QueueActionType,
    QueueTab,
    queue_confirm_keyboard,
    queue_item_keyboard,
)
from app.config import Settings
from app.domain.enums import NewsStatus
from app.publisher.stage_publish import ManualOutcome, ManualPublishDeps, ManualPublishResult
from tests.bot_helpers import FakeSession, callback_update, make_bot
from tests.config_helpers import bot_config
from tests.unit.test_bot_queue import NOW, Db, _deps

ADMIN = 111
NEWS_ID = 42
CALLBACK_DATA_LIMIT = 64


class FakePublishNow:
    """Подменяет publish_now: возвращает заданный итог и запоминает вызовы."""

    def __init__(self, result: ManualPublishResult, session: FakeSession) -> None:
        self.result = result
        self.session = session
        self.calls: list[tuple[int, datetime, int]] = []
        self.requests_before: list[Any] = []

    async def __call__(
        self, news_id: int, now: datetime, deps: ManualPublishDeps, initiator_id: int
    ) -> ManualPublishResult:
        self.calls.append((news_id, now, initiator_id))
        self.requests_before = list(self.session.requests)
        return self.result


def _cb(action: QueueActionType, tab: QueueTab = QueueTab.QUEUE) -> Update:
    data = QueueAction(action=action, tab=tab, page=1, news_id=NEWS_ID).pack()
    return callback_update(ADMIN, data)


async def _feed(
    update: Update,
    monkeypatch: pytest.MonkeyPatch,
    result: ManualPublishResult | None = None,
    db: Db | None = None,
) -> tuple[FakeSession, FakePublishNow]:
    class NoAdmins:
        async def is_admin(self, telegram_id: int) -> bool:
            return False

    bot, session = make_bot()
    fake = FakePublishNow(
        result or ManualPublishResult(ManualOutcome.PUBLISHED, NEWS_ID, message_id=9001), session
    )
    monkeypatch.setattr(queue_handlers, "publish_now", fake)
    deps = BotDeps(
        settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
        admin_repo=NoAdmins(),
        routers=[queue_handlers.create_router()],
        workflow_data={"queue_deps": _deps(db or Db())},
    )
    await create_dispatcher(deps).feed_update(bot, update)
    return session, fake


def _of(session: FakeSession, kind: type) -> list[Any]:
    return [r for r in session.requests if isinstance(r, kind)]


async def test_publish_shows_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    session, fake = await _feed(_cb(QueueActionType.PUBLISH), monkeypatch)
    [edit] = _of(session, EditMessageReplyMarkup)
    assert edit.reply_markup == queue_confirm_keyboard(NEWS_ID, QueueTab.QUEUE, 1)
    [answer] = _of(session, AnswerCallbackQuery)
    assert answer.text == texts.QUEUE_CONFIRM
    assert fake.calls == []


async def test_publish_warns_when_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Db()
    db.settings["paused"] = True
    session, _ = await _feed(_cb(QueueActionType.PUBLISH), monkeypatch, db=db)
    [answer] = _of(session, AnswerCallbackQuery)
    assert answer.text == f"{texts.QUEUE_CONFIRM} {texts.QUEUE_CONFIRM_PAUSED}"


async def test_cancel_restores_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    session, fake = await _feed(_cb(QueueActionType.CANCEL, QueueTab.REVIEW), monkeypatch)
    [edit] = _of(session, EditMessageReplyMarkup)
    assert edit.reply_markup == queue_item_keyboard(NEWS_ID, QueueTab.REVIEW, 1)
    assert fake.calls == []


async def test_confirm_answers_before_publishing(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = await _feed(_cb(QueueActionType.CONFIRM), monkeypatch)
    assert fake.calls == [(NEWS_ID, NOW, ADMIN)]
    answered = [r for r in fake.requests_before if isinstance(r, AnswerCallbackQuery)]
    assert [a.text for a in answered] == [texts.QUEUE_PUBLISHING]


def _marked(session: FakeSession) -> EditMessageText:
    [edit] = [r for r in session.requests if isinstance(r, EditMessageText)]
    return edit


async def test_confirm_published(monkeypatch: pytest.MonkeyPatch) -> None:
    with structlog.testing.capture_logs() as logs:
        session, _ = await _feed(_cb(QueueActionType.CONFIRM), monkeypatch)
    edit = _marked(session)
    assert edit.text is not None
    assert edit.text.endswith(f"<b>{texts.QUEUE_PUBLISHED}</b> — {ADMIN}")
    assert edit.reply_markup is None
    [entry] = [e for e in logs if e["event"] == "queue.publish_now"]
    assert (entry["news_id"], entry["user_id"], entry["outcome"]) == (NEWS_ID, ADMIN, "published")


async def test_confirm_busy_keeps_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ManualPublishResult(ManualOutcome.BUSY, NEWS_ID, status=NewsStatus.APPROVED)
    session, _ = await _feed(_cb(QueueActionType.CONFIRM), monkeypatch, result)
    assert texts.QUEUE_PUBLISH_BUSY in session.sent_texts()
    assert _of(session, EditMessageText) == []
    assert _of(session, EditMessageReplyMarkup) == []


@pytest.mark.parametrize(
    ("result", "decision"),
    [
        (
            ManualPublishResult(ManualOutcome.ALREADY_DONE, NEWS_ID, status=NewsStatus.PUBLISHED),
            "Уже обработано: published",
        ),
        (ManualPublishResult(ManualOutcome.NOT_FOUND, NEWS_ID), texts.QUEUE_PUBLISH_NOT_FOUND),
        (
            ManualPublishResult(
                ManualOutcome.FAILED,
                NEWS_ID,
                status=NewsStatus.FAILED,
                error="Telegram отклонил пост: chat not found",
            ),
            "❌ Не удалось опубликовать: Telegram отклонил пост: chat not found",
        ),
    ],
)
async def test_confirm_other_outcomes_mark_message(
    monkeypatch: pytest.MonkeyPatch, result: ManualPublishResult, decision: str
) -> None:
    session, _ = await _feed(_cb(QueueActionType.CONFIRM), monkeypatch, result)
    edit = _marked(session)
    assert edit.text is not None
    assert edit.text.endswith(f"<b>{decision}</b> — {ADMIN}")
    assert edit.reply_markup is None
    assert texts.QUEUE_PUBLISH_BUSY not in [m.text for m in _of(session, SendMessage)]


async def test_double_confirm_second_is_already_done(monkeypatch: pytest.MonkeyPatch) -> None:
    first, _ = await _feed(_cb(QueueActionType.CONFIRM), monkeypatch)
    done = ManualPublishResult(ManualOutcome.ALREADY_DONE, NEWS_ID, status=NewsStatus.PUBLISHED)
    second, fake = await _feed(_cb(QueueActionType.CONFIRM), monkeypatch, done)
    assert texts.QUEUE_PUBLISHED in (_marked(first).text or "")
    assert "Уже обработано: published" in (_marked(second).text or "")
    assert len(fake.calls) == 1


def test_new_buttons_callback_data_fits_limit() -> None:
    big = 2**63 - 1
    markups: list[InlineKeyboardMarkup] = [
        queue_item_keyboard(big, QueueTab.REVIEW, 10_000),
        queue_confirm_keyboard(big, QueueTab.REVIEW, 10_000),
    ]
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                assert len((button.callback_data or "").encode()) <= CALLBACK_DATA_LIMIT
    labels = [b.text for m in markups for row in m.inline_keyboard for b in row]
    assert labels == ["🚀 Опубликовать сейчас", "◀️ К списку", "✅ Да, опубликовать", "Отмена"]
