from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
)
from aiogram.types import InlineKeyboardMarkup, Update

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import errors as errors_handlers
from app.bot.handlers.errors import MUTED_KINDS_SETTING, ErrorsDeps, render_page
from app.bot.keyboards import ErrorAction, ErrorActionType, ErrorTab
from app.config import Settings
from app.db.models import Incident, NewsItem, ProcessedPost, Source
from app.domain.alerts import AlertKind
from app.domain.enums import NewsStatus, SourceType
from app.pipeline.retry import RetryDeps
from tests.bot_helpers import FakeSession, callback_update, make_bot, message_update
from tests.config_helpers import bot_config

ADMIN = 111
PAGE_SIZE = 5
TZ = "Europe/Moscow"
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)  # 12:00 по Москве
TEXT_LIMIT = 4096
PREVIEW = 80
SILENT = frozenset({AlertKind.AI_ITEM_FAILED})
CALLBACK_DATA_LIMIT = 64
S = NewsStatus


@dataclass
class Db:
    incidents: dict[int, Incident] = field(default_factory=dict)
    news: dict[int, NewsItem] = field(default_factory=dict)
    posts: dict[int, ProcessedPost] = field(default_factory=dict)
    sources: dict[int, Source] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    next_id: int = 0

    def incident(
        self,
        kind: AlertKind = AlertKind.SOURCE_DOWN,
        message: str = "Источник не отвечает",
        count: int = 1,
        resolved_at: datetime | None = None,
        last_seen_at: datetime | None = None,
    ) -> Incident:
        self.next_id += 1
        item = Incident(
            id=self.next_id,
            kind=kind.value,
            key=f"{kind.value}:{self.next_id}",
            message=message,
            count=count,
            first_seen_at=NOW - timedelta(hours=2),
            last_seen_at=last_seen_at or NOW,
            resolved_at=resolved_at,
            resolved_by=ADMIN if resolved_at else None,
        )
        self.incidents[item.id] = item
        return item

    def failed_news(
        self, reason: str = "bad_ai_response", with_post: bool = False, title: str = "Новость"
    ) -> NewsItem:
        if 1 not in self.sources:
            self.sources[1] = Source(
                id=1, type=SourceType.RSS, url="https://e.example/rss", name="Пример", enabled=True
            )
        self.next_id += 1
        item = NewsItem(
            id=self.next_id,
            source_id=1,
            external_id=str(self.next_id),
            url=f"https://e.example/{self.next_id}",
            title=title,
            raw_text="текст",
            status=S.FAILED,
            reject_reason=reason,
        )
        self.news[item.id] = item
        if with_post:
            self.posts[item.id] = ProcessedPost(
                news_item_id=item.id,
                ai_title="Заголовок",
                ai_summary="Пересказ",
                hashtags=[],
                relevance_score=0.8,
                prompt_version="v3",
                prompt_tokens=1,
                completion_tokens=1,
                cost=Decimal("0.001"),
            )
        return item


class IncidentRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    def _ordered(self, open_only: bool) -> list[Incident]:
        items = [i for i in self.db.incidents.values() if not open_only or i.resolved_at is None]
        return sorted(items, key=lambda i: (i.last_seen_at, i.id), reverse=True)

    async def get(self, incident_id: int) -> Incident | None:
        return self.db.incidents.get(incident_id)

    async def list(self, open_only: bool, limit: int, offset: int) -> Sequence[Incident]:
        return self._ordered(open_only)[offset : offset + limit]

    async def count(self, open_only: bool) -> int:
        return len(self._ordered(open_only))

    async def count_open_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self._ordered(open_only=True):
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return counts

    async def resolve(self, incident_id: int, by: int, at: datetime) -> bool:
        item = self.db.incidents.get(incident_id)
        if item is None or item.resolved_at is not None:
            return False
        item.resolved_at, item.resolved_by = at, by
        return True

    async def resolve_all(self, by: int, at: datetime, kind: str | None = None) -> int:
        closed = 0
        for item in self.db.incidents.values():
            if item.resolved_at is None and (kind is None or item.kind == kind):
                item.resolved_at, item.resolved_by = at, by
                closed += 1
        return closed


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    def _failed(self) -> list[NewsItem]:
        items = [n for n in self.db.news.values() if n.status is S.FAILED]
        return sorted(items, key=lambda n: n.id, reverse=True)

    async def list_failed(self, limit: int, offset: int) -> list[tuple[NewsItem, bool]]:
        chunk = self._failed()[offset : offset + limit]
        return [(n, n.id in self.db.posts) for n in chunk]

    async def count_failed(self) -> int:
        return len(self._failed())

    async def get(self, news_id: int) -> NewsItem | None:
        return self.db.news.get(news_id)

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        assert self.db.news[news_id].status == expected
        self.db.news[news_id].status = new


class PostRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        return self.db.posts.get(news_id)


class SourceRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, source_id: int) -> Source | None:
        return self.db.sources.get(source_id)


class SettingsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, key: str, default: Any = None) -> Any:
        return self.db.settings.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self.db.settings[key] = value


def _deps(db: Db) -> ErrorsDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    return ErrorsDeps(
        session_scope=scope,
        incident_repo=IncidentRepo,
        news_repo=NewsRepo,
        source_repo=SourceRepo,
        settings_repo=SettingsRepo,
        retry=RetryDeps(session_scope=scope, news_repo=NewsRepo, post_repo=PostRepo),
        silent_kinds=SILENT,
        page_size=PAGE_SIZE,
        preview_chars=PREVIEW,
        text_limit=TEXT_LIMIT,
        tz=TZ,
        clock=lambda: NOW,
    )


async def _feed(db: Db, update: Update) -> FakeSession:
    class NoAdmins:
        async def is_admin(self, telegram_id: int) -> bool:
            return False

    deps = BotDeps(
        settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
        admin_repo=NoAdmins(),
        routers=[errors_handlers.create_router()],
        workflow_data={"errors_deps": _deps(db)},
    )
    bot, session = make_bot()
    await create_dispatcher(deps).feed_update(bot, update)
    return session


def _cb(
    action: ErrorActionType, tab: ErrorTab = ErrorTab.OPEN, page: int = 0, item_id: int = 0
) -> Update:
    return callback_update(ADMIN, ErrorAction(action=action, tab=tab, page=page, id=item_id).pack())


def _of(session: FakeSession, kind: type) -> list[Any]:
    return [r for r in session.requests if isinstance(r, kind)]


def _markup(value: Any) -> InlineKeyboardMarkup:
    assert isinstance(value, InlineKeyboardMarkup)
    return value


def _buttons(markup: Any) -> list[tuple[str, ErrorAction]]:
    return [
        (b.text, ErrorAction.unpack(b.callback_data or ""))
        for row in _markup(markup).inline_keyboard
        for b in row
    ]


def _items(text: str) -> list[str]:
    return [line for line in text.splitlines() if line[:1].isdigit()]


def test_alert_kind_labels_cover_all_kinds() -> None:
    assert set(texts.ALERT_KIND_LABELS) == {kind.value for kind in AlertKind}


async def test_open_tab_lists_only_open_with_counts() -> None:
    db = Db()
    db.incident(AlertKind.SOURCE_DOWN, count=3)
    db.incident(AlertKind.AI_DOWN, last_seen_at=NOW - timedelta(minutes=1))
    db.incident(AlertKind.PUBLISH_ERROR, resolved_at=NOW)

    text, keyboard = await render_page(_deps(db), ErrorTab.OPEN, 0)
    assert text.startswith("<b>Ошибки</b> · открыто 2")
    assert "модель недоступна 1" in text and "источник не отвечает 1" in text
    lines = _items(text)
    assert lines[0] == "1. 🔴 источник не отвечает ×3 · 12:00 — Источник не отвечает"
    assert len(lines) == 2
    assert any(label == "✅ Решить все" for label, _ in _buttons(keyboard))


async def test_all_tab_marks_resolved() -> None:
    db = Db()
    db.incident(AlertKind.SOURCE_DOWN)
    db.incident(AlertKind.AI_DOWN, resolved_at=NOW, last_seen_at=NOW - timedelta(minutes=1))

    text, keyboard = await render_page(_deps(db), ErrorTab.ALL, 0)
    lines = _items(text)
    assert len(lines) == 2
    assert lines[1].endswith("✅")
    assert not any(label == "✅ Решить все" for label, _ in _buttons(keyboard))
    tabs = [label for label, a in _buttons(keyboard) if a.action == "page" and a.page == 0]
    assert tabs == ["Открытые", "• Все", "Новости"]


async def test_empty_journal() -> None:
    text, keyboard = await render_page(_deps(Db()), ErrorTab.OPEN, 0)
    assert texts.ERRORS_EMPTY in text
    assert [label for label, _ in _buttons(keyboard)] == ["• Открытые", "Все", "Новости"]


async def test_pagination_and_clamping() -> None:
    db = Db()
    for index in range(12):
        db.incident(last_seen_at=NOW - timedelta(minutes=index))
    deps = _deps(db)

    text, keyboard = await render_page(deps, ErrorTab.OPEN, 0)
    assert "страница 1 из 3" in text
    assert len(_items(text)) == PAGE_SIZE
    assert [label for label, _ in _buttons(keyboard) if label in {"◀️", "▶️"}] == ["▶️"]

    text, _ = await render_page(deps, ErrorTab.OPEN, 1)
    assert _items(text)[0].startswith("6.")
    text, _ = await render_page(deps, ErrorTab.OPEN, 99)
    assert "страница 3 из 3" in text


async def test_message_escaped_and_truncated() -> None:
    db = Db()
    db.incident(message="<b>сбой</b> & авария " + "очень длинное описание " * 20)
    text, _ = await render_page(_deps(db), ErrorTab.OPEN, 0)
    [line] = _items(text)
    assert "&lt;b&gt;сбой&lt;/b&gt; &amp;" in line
    assert "<b>сбой" not in line
    assert line.endswith("…")


async def test_news_tab_lists_failed() -> None:
    db = Db()
    db.failed_news(reason="bad_ai_response", title="Первая")
    db.failed_news(reason="publish_error", with_post=True, title="Вторая")

    text, keyboard = await render_page(_deps(db), ErrorTab.NEWS, 0)
    assert text.startswith("<b>Новости с ошибкой</b> · 2")
    assert _items(text) == [
        "1. Вторая — Пример · не удалось опубликовать",
        "2. Первая — Пример · модель вернула некорректный ответ",
    ]
    assert not any(label == "✅ Решить все" for label, _ in _buttons(keyboard))


async def test_news_tab_empty() -> None:
    text, _ = await render_page(_deps(Db()), ErrorTab.NEWS, 0)
    assert texts.ERRORS_NEWS_EMPTY in text


async def test_command_shows_open_tab() -> None:
    db = Db()
    db.incident()
    session = await _feed(db, message_update(ADMIN, "/errors"))
    [sent] = session.sent()
    assert sent.text.startswith("<b>Ошибки</b> · открыто 1")


async def test_page_callback_edits_list() -> None:
    db = Db()
    db.incident()
    session = await _feed(db, _cb(ErrorActionType.PAGE, tab=ErrorTab.ALL))
    [edit] = _of(session, EditMessageText)
    assert "<b>Ошибки</b>" in (edit.text or "")


async def test_open_incident_card() -> None:
    db = Db()
    incident = db.incident(count=4, message="Источник «A & B» не отвечает")
    session = await _feed(db, _cb(ErrorActionType.OPEN, item_id=incident.id))
    [sent] = session.sent()
    assert "<b>источник не отвечает</b>" in sent.text
    assert f"<code>{incident.key}</code>" in sent.text
    assert "Повторов: 4" in sent.text
    assert f"Статус: {texts.ERRORS_OPEN_STATUS}" in sent.text
    assert "Источник «A &amp; B» не отвечает" in sent.text
    labels = [label for label, _ in _buttons(sent.reply_markup)]
    assert labels == ["✅ Решено", "🔕 Не присылать в чат", "◀️ К списку"]


async def test_resolved_card_has_no_resolve_button() -> None:
    db = Db()
    incident = db.incident(resolved_at=NOW)
    session = await _feed(db, _cb(ErrorActionType.OPEN, tab=ErrorTab.ALL, item_id=incident.id))
    [sent] = session.sent()
    assert "Статус: решена" in sent.text
    assert "✅ Решено" not in [label for label, _ in _buttons(sent.reply_markup)]


async def test_resolve_then_resolve_again() -> None:
    db = Db()
    incident = db.incident()
    session = await _feed(db, _cb(ErrorActionType.RESOLVE, item_id=incident.id))
    [edit] = _of(session, EditMessageText)
    assert "Статус: решена" in (edit.text or "")
    assert db.incidents[incident.id].resolved_by == ADMIN

    session = await _feed(db, _cb(ErrorActionType.RESOLVE, item_id=incident.id))
    [answer] = _of(session, AnswerCallbackQuery)
    assert answer.text == texts.ERRORS_ALREADY_RESOLVED
    assert answer.show_alert is True


async def test_resolve_all_with_confirmation() -> None:
    db = Db()
    db.incident()
    db.incident(AlertKind.AI_DOWN)

    session = await _feed(db, _cb(ErrorActionType.RESOLVE_ALL))
    [edit] = _of(session, EditMessageText)
    assert edit.text == texts.ERRORS_CONFIRM_ALL
    assert [label for label, _ in _buttons(edit.reply_markup)] == ["Да, закрыть все", "Отмена"]
    assert all(i.resolved_at is None for i in db.incidents.values())

    session = await _feed(db, _cb(ErrorActionType.CONFIRM_ALL))
    [answer] = _of(session, AnswerCallbackQuery)
    assert answer.text == texts.ERRORS_RESOLVED_COUNT.format(count=2)
    assert all(i.resolved_at is not None for i in db.incidents.values())


async def test_resolve_all_cancel_returns_list() -> None:
    db = Db()
    db.incident()
    session = await _feed(db, _cb(ErrorActionType.BACK))
    [sent] = session.sent()
    assert "<b>Ошибки</b>" in sent.text
    assert all(i.resolved_at is None for i in db.incidents.values())


async def test_mute_and_unmute_kind() -> None:
    db = Db()
    incident = db.incident(AlertKind.SOURCE_DOWN)

    session = await _feed(db, _cb(ErrorActionType.MUTE, item_id=incident.id))
    [edit] = _of(session, EditMessageText)
    assert texts.ERRORS_MUTED_MARK in (edit.text or "")
    assert db.settings[MUTED_KINDS_SETTING] == ["source_down"]
    assert "🔔 Присылать в чат" in [label for label, _ in _buttons(edit.reply_markup)]

    session = await _feed(db, _cb(ErrorActionType.MUTE, item_id=incident.id))
    [edit] = _of(session, EditMessageText)
    assert texts.ERRORS_MUTED_MARK not in (edit.text or "")
    assert db.settings[MUTED_KINDS_SETTING] == []


async def test_silent_kind_cannot_be_unmuted() -> None:
    db = Db()
    incident = db.incident(AlertKind.AI_ITEM_FAILED)
    session = await _feed(db, _cb(ErrorActionType.MUTE, item_id=incident.id))
    [answer] = _of(session, AnswerCallbackQuery)
    assert answer.text == texts.ERRORS_KIND_FROM_CONFIG
    assert MUTED_KINDS_SETTING not in db.settings


async def test_silent_kind_card_shows_mark() -> None:
    db = Db()
    incident = db.incident(AlertKind.AI_ITEM_FAILED)
    session = await _feed(db, _cb(ErrorActionType.OPEN, item_id=incident.id))
    [sent] = session.sent()
    assert texts.ERRORS_MUTED_MARK in sent.text


async def test_open_news_card() -> None:
    db = Db()
    news = db.failed_news(reason="publish_error", with_post=True, title="Сбой <публикации>")
    session = await _feed(db, _cb(ErrorActionType.OPEN, tab=ErrorTab.NEWS, item_id=news.id))
    [sent] = session.sent()
    assert "<b>Сбой &lt;публикации&gt;</b>" in sent.text
    assert "Причина: не удалось опубликовать" in sent.text
    assert texts.RETRY_HINT_TO_QUEUE in sent.text
    assert [label for label, _ in _buttons(sent.reply_markup)] == ["🔁 Повторить", "◀️ К списку"]


async def test_news_card_explains_unknown_publication() -> None:
    """Зависшая отправка: админ должен проверить канал перед «Повторить»."""
    db = Db()
    news = db.failed_news(reason="publish_unknown", with_post=True)
    session = await _feed(db, _cb(ErrorActionType.OPEN, tab=ErrorTab.NEWS, item_id=news.id))
    [sent] = session.sent()
    assert "Причина: неизвестно, вышел ли пост: проверьте канал" in sent.text
    assert texts.RETRY_HINT_TO_QUEUE in sent.text


@pytest.mark.parametrize(
    ("with_post", "expected"),
    [(False, "to_ai"), (True, "to_queue")],
)
async def test_retry_outcomes(with_post: bool, expected: str) -> None:
    db = Db()
    news = db.failed_news(with_post=with_post)
    session = await _feed(db, _cb(ErrorActionType.RETRY, tab=ErrorTab.NEWS, item_id=news.id))
    assert texts.RETRY_RESULTS[expected] in session.sent_texts()
    assert _of(session, EditMessageReplyMarkup)
    assert db.news[news.id].status is (S.APPROVED if with_post else S.NORMALIZED)


async def test_retry_already_done_by_other_admin() -> None:
    db = Db()
    news = db.failed_news()
    db.news[news.id].status = S.NORMALIZED
    session = await _feed(db, _cb(ErrorActionType.RETRY, tab=ErrorTab.NEWS, item_id=news.id))
    assert texts.RETRY_RESULTS["not_failed"].format(status="normalized") in session.sent_texts()
    assert not _of(session, EditMessageReplyMarkup)


async def test_retry_missing_news() -> None:
    session = await _feed(Db(), _cb(ErrorActionType.RETRY, tab=ErrorTab.NEWS, item_id=404))
    assert texts.RETRY_RESULTS["not_found"] in session.sent_texts()


async def test_long_message_fits_telegram_limit() -> None:
    db = Db()
    incident = db.incident(message="я" * 6000)
    session = await _feed(db, _cb(ErrorActionType.OPEN, item_id=incident.id))
    [sent] = session.sent()
    assert len(sent.text) <= TEXT_LIMIT


async def test_callback_data_fits_limit() -> None:
    db = Db()
    db.next_id = 2**62
    incident = db.incident()
    session = await _feed(db, _cb(ErrorActionType.OPEN, tab=ErrorTab.ALL, item_id=incident.id))
    [sent] = session.sent()
    _, list_keyboard = await render_page(_deps(db), ErrorTab.ALL, 0)
    for markup in (_markup(sent.reply_markup), list_keyboard):
        for row in markup.inline_keyboard:
            for button in row:
                assert len((button.callback_data or "").encode()) <= CALLBACK_DATA_LIMIT


def test_real_components_fit_protocols() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.incidents import IncidentRepository
    from app.db.repositories.misc import SettingsRepository
    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository
    from app.db.repositories.sources import SourceRepository

    deps = ErrorsDeps(
        session_scope=MagicMock(),
        incident_repo=IncidentRepository,
        news_repo=NewsRepository,
        source_repo=SourceRepository,
        settings_repo=SettingsRepository,
        retry=RetryDeps(
            session_scope=MagicMock(),
            news_repo=NewsRepository,
            post_repo=ProcessedPostRepository,
        ),
        silent_kinds=SILENT,
        page_size=PAGE_SIZE,
        preview_chars=PREVIEW,
        text_limit=TEXT_LIMIT,
        tz=TZ,
        clock=lambda: NOW,
    )
    assert deps.page_size == PAGE_SIZE
