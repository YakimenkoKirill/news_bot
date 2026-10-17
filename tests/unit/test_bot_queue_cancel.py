from typing import Any

import pytest
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
    cancel_all_confirm_keyboard,
    cancel_confirm_keyboard,
    queue_item_keyboard,
)
from app.config import Settings
from app.domain.enums import NewsStatus
from tests.bot_helpers import FakeSession, callback_update, make_bot
from tests.config_helpers import bot_config
from tests.unit.test_bot_queue import NOW, Db, _deps

ADMIN = 111
PAGE = 1
CALLBACK_DATA_LIMIT = 64
S = NewsStatus


def _cb(
    action: QueueActionType,
    tab: QueueTab = QueueTab.REVIEW,
    news_id: int = 1,
    page: int = PAGE,
) -> Update:
    data = QueueAction(action=action, tab=tab, page=page, news_id=news_id).pack()
    return callback_update(ADMIN, data)


async def _feed(update: Update, db: Db) -> FakeSession:
    class NoAdmins:
        async def is_admin(self, telegram_id: int) -> bool:
            return False

    deps = BotDeps(
        settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
        admin_repo=NoAdmins(),
        routers=[queue_handlers.create_router()],
        workflow_data={"queue_deps": _deps(db)},
    )
    bot, session = make_bot()
    await create_dispatcher(deps).feed_update(bot, update)
    return session


def _of(session: FakeSession, kind: type) -> list[Any]:
    return [r for r in session.requests if isinstance(r, kind)]


def _review_db(count: int = 2) -> Db:
    db = Db()
    for news_id in range(1, count + 1):
        db.add(news_id, 0.8, status=S.PENDING_REVIEW)
    return db


def test_cancel_button_only_on_review_tab() -> None:
    review = queue_item_keyboard(1, QueueTab.REVIEW, PAGE)
    queue = queue_item_keyboard(1, QueueTab.QUEUE, PAGE)

    def labels(markup: InlineKeyboardMarkup) -> list[str]:
        return [button.text for row in markup.inline_keyboard for button in row]

    assert texts.QUEUE_CANCEL_BUTTON in labels(review)
    assert texts.QUEUE_CANCEL_BUTTON not in labels(queue)
    assert labels(review)[-1] == "◀️ К списку"


def _page_buttons(session: FakeSession) -> list[str]:
    """Подписи кнопок страницы списка (страница приходит правкой сообщения)."""
    [edit] = _of(session, EditMessageText)
    markup = edit.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button.text for row in markup.inline_keyboard for button in row]


async def test_cancel_all_button_only_on_non_empty_review() -> None:
    db = _review_db()
    review = await _feed(_cb(QueueActionType.PAGE, QueueTab.REVIEW, page=0), db)
    assert texts.QUEUE_CANCEL_ALL_BUTTON in _page_buttons(review)

    queue = await _feed(_cb(QueueActionType.PAGE, QueueTab.QUEUE, page=0), db)
    assert texts.QUEUE_CANCEL_ALL_BUTTON not in _page_buttons(queue)

    empty = await _feed(_cb(QueueActionType.PAGE, QueueTab.REVIEW, page=0), Db())
    assert texts.QUEUE_CANCEL_ALL_BUTTON not in _page_buttons(empty)


async def test_cancel_asks_confirmation() -> None:
    db = _review_db()
    session = await _feed(_cb(QueueActionType.CANCEL_REVIEW), db)

    [edit] = _of(session, EditMessageReplyMarkup)
    assert edit.reply_markup == cancel_confirm_keyboard(1, QueueTab.REVIEW, PAGE)
    [answer] = _of(session, AnswerCallbackQuery)
    assert answer.text == texts.QUEUE_CANCEL_CONFIRM
    assert db.news[1].status is S.PENDING_REVIEW, "до подтверждения ничего не меняется"


async def test_no_restores_item_buttons() -> None:
    db = _review_db()
    session = await _feed(_cb(QueueActionType.CANCEL), db)

    [edit] = _of(session, EditMessageReplyMarkup)
    assert edit.reply_markup == queue_item_keyboard(1, QueueTab.REVIEW, PAGE)
    assert db.news[1].status is S.PENDING_REVIEW


async def test_confirm_cancels_post() -> None:
    db = _review_db()
    session = await _feed(_cb(QueueActionType.CONFIRM_CANCEL_REVIEW), db)

    assert db.news[1].status is S.REJECTED
    assert db.news[1].reject_reason == "rejected_by_moderator"
    [edit] = _of(session, EditMessageText)
    assert texts.QUEUE_CANCELLED in (edit.text or "")
    assert str(ADMIN) in (edit.text or "")
    assert edit.reply_markup is None


async def test_confirm_clears_snooze() -> None:
    """Отложенный «Позже» пост после отмены не вернётся на модерацию."""
    db = _review_db()
    db.posts[1].snoozed_until = NOW
    await _feed(_cb(QueueActionType.CONFIRM_CANCEL_REVIEW), db)
    assert db.posts[1].snoozed_until is None


async def test_confirm_when_already_approved() -> None:
    """Пост одобрили в другом окне между открытием и подтверждением."""
    db = _review_db()
    db.news[1].status = S.APPROVED
    session = await _feed(_cb(QueueActionType.CONFIRM_CANCEL_REVIEW), db)

    [edit] = _of(session, EditMessageText)
    assert texts.QUEUE_CANCEL_DONE.format(status="approved") in (edit.text or "")
    assert edit.reply_markup is None
    assert db.news[1].status is S.APPROVED


async def test_confirm_when_post_deleted() -> None:
    session = await _feed(_cb(QueueActionType.CONFIRM_CANCEL_REVIEW, news_id=99), Db())
    [edit] = _of(session, EditMessageText)
    assert texts.QUEUE_CANCEL_NOT_FOUND in (edit.text or "")


async def test_cancel_all_asks_confirmation_with_count() -> None:
    db = _review_db(count=3)
    session = await _feed(_cb(QueueActionType.CANCEL_ALL), db)

    [edit] = _of(session, EditMessageText)
    assert edit.text == texts.QUEUE_CANCEL_ALL_CONFIRM.format(count=3)
    assert edit.reply_markup == cancel_all_confirm_keyboard(QueueTab.REVIEW, PAGE)
    assert all(news.status is S.PENDING_REVIEW for news in db.news.values())


async def test_cancel_all_no_returns_list() -> None:
    """Кнопка «Нет» в подтверждении отмены всех — это возврат к списку."""
    db = _review_db()
    session = await _feed(_cb(QueueActionType.PAGE, QueueTab.REVIEW, page=PAGE), db)
    [edit] = _of(session, EditMessageText)
    assert texts.QUEUE_TITLES["review"] in (edit.text or "")
    assert all(news.status is S.PENDING_REVIEW for news in db.news.values())


async def test_confirm_cancel_all_cancels_everything() -> None:
    db = _review_db(count=3)
    db.add(4, 0.9, status=S.APPROVED)
    session = await _feed(_cb(QueueActionType.CONFIRM_CANCEL_ALL), db)

    assert [db.news[i].status for i in (1, 2, 3)] == [S.REJECTED] * 3
    assert db.news[4].status is S.APPROVED
    texts_sent = [m.text for m in _of(session, SendMessage)]
    assert texts.QUEUE_CANCELLED_ALL.format(count=3) in texts_sent
    assert any(texts.QUEUE_TITLES["review"] in text for text in texts_sent)


async def test_confirm_cancel_all_answers_callback_first() -> None:
    """Отмена многих постов идёт после ответа на callback: у Telegram свой таймаут."""
    db = _review_db(count=3)
    session = await _feed(_cb(QueueActionType.CONFIRM_CANCEL_ALL), db)

    kinds = [type(request) for request in session.requests]
    assert kinds[0] is AnswerCallbackQuery
    assert SendMessage in kinds


async def test_confirm_cancel_all_on_empty_review() -> None:
    session = await _feed(_cb(QueueActionType.CONFIRM_CANCEL_ALL), Db())
    assert texts.QUEUE_CANCELLED_ALL.format(count=0) in [m.text for m in _of(session, SendMessage)]


@pytest.mark.parametrize(
    "action",
    [
        QueueActionType.CANCEL_REVIEW,
        QueueActionType.CONFIRM_CANCEL_REVIEW,
        QueueActionType.CANCEL_ALL,
        QueueActionType.CONFIRM_CANCEL_ALL,
    ],
)
def test_callback_data_fits_telegram_limit(action: QueueActionType) -> None:
    packed = QueueAction(action=action, tab=QueueTab.REVIEW, page=10_000, news_id=2**63 - 1).pack()
    assert len(packed.encode()) <= CALLBACK_DATA_LIMIT, packed
