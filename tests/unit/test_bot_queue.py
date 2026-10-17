from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, SendPhoto
from aiogram.types import InlineKeyboardMarkup, Update

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import queue as queue_handlers
from app.bot.handlers.queue import QueueDeps, render_page
from app.bot.keyboards import QueueAction, QueueActionType, QueueTab, queue_item_keyboard
from app.bot.moderation import REVIEW_NOTE_PREFIX
from app.bot.preview import PreviewDeps
from app.config import PublisherConfig, Settings
from app.db.models import NewsItem, ProcessedPost, Source
from app.db.repositories.news import Priority
from app.domain.enums import NewsStatus, SourceType
from app.publisher.media import ImageCheck
from app.publisher.stage_publish import ManualPublishDeps
from tests.bot_helpers import FakeSession, callback_update, make_bot, message_update
from tests.config_helpers import base_section, bot_config

S = NewsStatus
ADMIN = 111
PAGE_SIZE = 5
CHANNEL = -100777
TZ = "Europe/Moscow"
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)  # 12:00 по Москве
DECAY = 0.02
CFG = PublisherConfig(
    **{**base_section("publisher"), "quiet_hours": ["23:00", "07:00"], "score_decay_per_h": DECAY}
)
SECONDS_PER_HOUR = 3600
INTERVAL = timedelta(minutes=CFG.min_interval_min)
CALLBACK_DATA_LIMIT = 64
MAX_PER_SOURCE = 0  # по умолчанию заполненность не показываем


@dataclass
class Db:
    news: dict[int, NewsItem] = field(default_factory=dict)
    posts: dict[int, ProcessedPost] = field(default_factory=dict)
    sources: dict[int, Source] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    last_published: datetime | None = None

    def add(
        self,
        news_id: int,
        score: float,
        *,
        status: NewsStatus = S.APPROVED,
        published_at: datetime | None = None,
        title: str | None = None,
        review_note: str | None = None,
        image_url: str | None = "https://cdn.example/a.jpg",
        cluster_sources: int = 1,
    ) -> None:
        if 1 not in self.sources:
            self.sources[1] = Source(
                id=1, type=SourceType.RSS, url="https://s.example/rss", name="Пример", enabled=True
            )
        self.news[news_id] = NewsItem(
            id=news_id,
            source_id=1,
            external_id=str(news_id),
            url=f"https://s.example/{news_id}",
            title="t",
            raw_text="r",
            status=status,
            image_url=image_url,
            published_at=published_at or NOW,
            created_at=NOW,
            cluster_sources=cluster_sources,
        )
        self.posts[news_id] = ProcessedPost(
            id=news_id * 10,
            news_item_id=news_id,
            ai_title=title or f"Заголовок {news_id}",
            ai_summary="Пересказ новости.",
            hashtags=["#тег"],
            relevance_score=score,
            prompt_version="v1",
            prompt_tokens=1,
            completion_tokens=1,
            cost=Decimal("0.001"),
            review_note=review_note,
        )


def _priority(db: Db, news: NewsItem, priority: Priority) -> float:
    moment = news.published_at or news.created_at
    bonus = priority.cluster_bonus * min(
        max(news.cluster_sources - 1, 0), priority.cluster_bonus_max
    )
    return (
        db.posts[news.id].relevance_score
        + priority.decay_per_h * moment.timestamp() / SECONDS_PER_HOUR
        + bonus
    )


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    def _ordered(self, statuses: Sequence[NewsStatus], priority: Priority) -> list[NewsItem]:
        items = [n for n in self.db.news.values() if n.status in statuses and n.id in self.db.posts]
        return sorted(items, key=lambda n: (-_priority(self.db, n, priority), n.id))

    async def list_prepared(
        self, statuses: Sequence[NewsStatus], priority: Priority, limit: int, offset: int
    ) -> list[tuple[NewsItem, ProcessedPost]]:
        chunk = self._ordered(statuses, priority)[offset : offset + limit]
        return [(n, self.db.posts[n.id]) for n in chunk]

    async def count_prepared(self, statuses: Sequence[NewsStatus]) -> int:
        return len(self._ordered(statuses, Priority(0)))

    async def queue_counts_by_source(self, statuses: Sequence[NewsStatus]) -> dict[int, int]:
        counts: dict[int, int] = {}
        for item in self._ordered(statuses, Priority(0)):
            counts[item.source_id] = counts.get(item.source_id, 0) + 1
        return counts

    async def claim_next(self, statuses: Sequence[NewsStatus], priority: Priority) -> NewsItem:
        return self._ordered(statuses, priority)[0]

    async def get(self, news_id: int) -> NewsItem | None:
        return self.db.news.get(news_id)


class PostRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        return self.db.posts.get(news_id)


class PublicationRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def last_published_at(self, channel_id: int) -> datetime | None:
        return self.db.last_published


class SettingsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, key: str, default: Any = None) -> Any:
        return self.db.settings.get(key, default)


class SourceRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, source_id: int) -> Source | None:
        return self.db.sources.get(source_id)


def _deps(db: Db, now: datetime = NOW) -> QueueDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    async def check(url: str | None) -> ImageCheck:
        return ImageCheck(ok=url is not None, reason=None if url else "no_image")

    preview = PreviewDeps(
        session_scope=scope,
        news_repo=NewsRepo,
        source_repo=SourceRepo,
        check_image=check,
        cfg=CFG,
    )
    return QueueDeps(
        session_scope=scope,
        news_repo=NewsRepo,
        post_repo=PostRepo,
        publication_repo=PublicationRepo,
        settings_repo=SettingsRepo,
        source_repo=SourceRepo,
        preview=preview,
        publisher_cfg=CFG,
        channel_id=CHANNEL,
        tz=TZ,
        page_size=PAGE_SIZE,
        title_chars=bot_config().queue_title_chars,
        max_per_source=MAX_PER_SOURCE,
        clock=lambda: now,
        manual=ManualPublishDeps(publish=MagicMock(), news_repo=MagicMock()),
    )


async def _feed(db: Db, update: Any, now: datetime = NOW) -> FakeSession:
    class NoAdmins:
        async def is_admin(self, telegram_id: int) -> bool:
            return False

    deps = BotDeps(
        settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
        admin_repo=NoAdmins(),
        routers=[queue_handlers.create_router()],
        workflow_data={"queue_deps": _deps(db, now)},
    )
    bot, session = make_bot()
    await create_dispatcher(deps).feed_update(bot, update)
    return session


def _cb(
    action: QueueActionType, tab: QueueTab = QueueTab.QUEUE, page: int = 0, news_id: int = 0
) -> Update:
    data = QueueAction(action=action, tab=tab, page=page, news_id=news_id).pack()
    return callback_update(ADMIN, data)


def _eta(moment: datetime) -> str:
    """Ожидаемое время выхода в поясе канала, как его печатает бот."""
    return f"≈{moment.astimezone(ZoneInfo(TZ)):%H:%M}"


def _items(text: str) -> list[str]:
    return [line for line in text.splitlines() if line[:1].isdigit()]


def _buttons(markup: InlineKeyboardMarkup) -> list[tuple[str, QueueAction]]:
    return [
        (button.text, QueueAction.unpack(button.callback_data or ""))
        for row in markup.inline_keyboard
        for button in row
    ]


def _db_with(count: int) -> Db:
    db = Db()
    for news_id in range(1, count + 1):
        db.add(news_id, round(0.99 - news_id * 0.01, 2))
    return db


async def test_order_matches_publish_selection() -> None:
    db = Db()
    db.add(1, 0.8, published_at=NOW - timedelta(hours=12))
    db.add(2, 0.6)
    db.add(3, 0.9, published_at=NOW - timedelta(hours=30))
    db.add(4, 0.7, published_at=NOW - timedelta(hours=1))
    text, _ = await render_page(_deps(db), QueueTab.QUEUE, 0, NOW)
    shown = [line.split(".")[0] for line in _items(text)]

    repo = NewsRepo(db)
    expected: list[int] = []
    while any(n.status is S.APPROVED for n in db.news.values()):
        chosen = await repo.claim_next([S.APPROVED, S.SCHEDULED], Priority.from_config(CFG))
        expected.append(chosen.id)
        chosen.status = S.PUBLISHED
    assert shown == ["1", "2", "3", "4"]
    titles = [line.split(". ", 1)[1].split(" — ")[0] for line in _items(text)]
    assert titles == [f"Заголовок {news_id}" for news_id in expected]
    assert expected == [4, 2, 1, 3]


async def test_sending_post_marked_instead_of_eta() -> None:
    """Пост в scheduled уже отправляется: вместо прогноза — «⏳ отправляется»."""
    db = Db()
    db.add(1, 0.9, status=S.SCHEDULED)
    db.add(2, 0.5)
    text, _ = await render_page(_deps(db), QueueTab.QUEUE, 0, NOW)
    lines = _items(text)
    assert lines[0] == f"1. Заголовок 1 — Пример · ★0.90 · {texts.QUEUE_SENDING}"
    assert lines[1] == f"2. Заголовок 2 — Пример · ★0.50 · {_eta(NOW + INTERVAL)}"


async def test_cluster_mark_shown_for_several_sources() -> None:
    """🔁N — о событии написали несколько источников."""
    db = Db()
    db.add(1, 0.9, cluster_sources=3)
    db.add(2, 0.5)
    text, _ = await render_page(_deps(db), QueueTab.QUEUE, 0, NOW)
    lines = _items(text)
    assert lines[0] == f"1. Заголовок 1 — Пример 🔁3 · ★0.90 · {_eta(NOW)}"
    assert "🔁" not in lines[1]


async def test_cluster_mark_hidden_for_single_source() -> None:
    db = Db()
    db.add(1, 0.9, cluster_sources=1)
    text, _ = await render_page(_deps(db), QueueTab.QUEUE, 0, NOW)
    assert "🔁" not in text


async def test_line_has_score_source_and_eta() -> None:
    db = Db()
    db.add(1, 0.876)
    db.add(2, 0.5)
    text, keyboard = await render_page(_deps(db), QueueTab.QUEUE, 0, NOW)
    lines = _items(text)
    assert lines[0] == f"1. Заголовок 1 — Пример · ★0.88 · {_eta(NOW)}"
    assert lines[1] == f"2. Заголовок 2 — Пример · ★0.50 · {_eta(NOW + INTERVAL)}"
    assert texts.queue_header("queue", 2, 1, 1) in text
    assert texts.QUEUE_HINT in text
    opens = [(label, a.news_id) for label, a in _buttons(keyboard) if a.action == "open"]
    assert opens == [("👁 1", 1), ("👁 2", 2)]


async def test_eta_respects_last_publication_and_quiet_hours() -> None:
    db = Db()
    db.add(1, 0.9)
    db.add(2, 0.8)
    db.last_published = NOW - timedelta(minutes=4)
    text, _ = await render_page(_deps(db), QueueTab.QUEUE, 0, NOW)
    first = NOW + timedelta(minutes=CFG.min_interval_min - 4)
    assert [line.rsplit(" ", 1)[1] for line in _items(text)] == [
        _eta(first),
        _eta(first + INTERVAL),
    ]

    late = datetime(2026, 10, 10, 19, 55, tzinfo=UTC)  # 22:55 по Москве
    db.last_published = None
    text, _ = await render_page(_deps(db, late), QueueTab.QUEUE, 0, late)
    lines = _items(text)
    assert lines[0].endswith("≈22:55")
    assert lines[1].endswith("≈11.10 07:00")


async def test_pagination_and_eta_continue_on_next_page() -> None:
    db = _db_with(12)
    text, keyboard = await render_page(_deps(db), QueueTab.QUEUE, 0, NOW)
    assert "страница 1 из 3" in text
    assert len(_items(text)) == PAGE_SIZE
    nav = [label for label, _ in _buttons(keyboard) if label in {"◀️", "▶️"}]
    assert nav == ["▶️"]

    text, keyboard = await render_page(_deps(db), QueueTab.QUEUE, 1, NOW)
    lines = _items(text)
    assert "страница 2 из 3" in text
    assert lines[0].startswith("6. Заголовок 6")
    assert lines[0].endswith(_eta(NOW + PAGE_SIZE * INTERVAL))  # шестой пост
    nav = [label for label, _ in _buttons(keyboard) if label in {"◀️", "▶️"}]
    assert nav == ["◀️", "▶️"]


async def test_page_past_end_shows_last_page() -> None:
    text, _ = await render_page(_deps(_db_with(7)), QueueTab.QUEUE, 9, NOW)
    assert "страница 2 из 2" in text
    assert _items(text)[0].startswith("6.")


async def test_paused_hides_eta() -> None:
    db = _db_with(2)
    db.settings["paused"] = True
    text, _ = await render_page(_deps(db), QueueTab.QUEUE, 0, NOW)
    assert texts.QUEUE_PAUSED in text
    assert all("≈" not in line for line in _items(text))


async def test_review_tab() -> None:
    db = Db()
    db.add(1, 0.4, status=S.PENDING_REVIEW, review_note="Не найдены в источнике: 45%")
    db.add(2, 0.9, status=S.PENDING_REVIEW)
    db.add(3, 0.95)
    text, keyboard = await render_page(_deps(db), QueueTab.REVIEW, 0, NOW)
    assert text.startswith("<b>На модерации</b> · 2 постов")
    assert _items(text) == ["1. Заголовок 2 — Пример · ★0.90", "2. Заголовок 1 — Пример · ★0.40 ⚠️"]
    assert texts.QUEUE_HINT not in text
    tabs = [label for label, a in _buttons(keyboard) if a.action == "page" and a.page == 0]
    assert tabs == ["В очереди", "• На модерации"]


async def test_empty_queue() -> None:
    text, keyboard = await render_page(_deps(Db()), QueueTab.QUEUE, 0, NOW)
    assert text == f"<b>Очередь публикации</b>\n{texts.QUEUE_EMPTY['queue']}"
    assert [label for label, _ in _buttons(keyboard)] == ["• В очереди", "На модерации"]


async def test_title_escaped_and_truncated() -> None:
    db = Db()
    long_title = "<b>Срочно</b> & " + "очень длинный заголовок " * 10
    db.add(1, 0.9, title=long_title)
    text, _ = await render_page(_deps(db), QueueTab.QUEUE, 0, NOW)
    [line] = _items(text)
    assert "&lt;b&gt;Срочно&lt;/b&gt; &amp;" in line
    assert "<b>Срочно" not in line
    assert "…" in line


async def test_callback_data_fits_limit() -> None:
    db = Db()
    big = 2**63 - 1
    db.add(big, 0.9)
    _, keyboard = await render_page(_deps(db), QueueTab.REVIEW, 0, NOW)
    item = queue_item_keyboard(big, QueueTab.REVIEW, 10_000)
    markup_buttons = [b for m in (keyboard, item) for row in m.inline_keyboard for b in row]
    data = QueueAction(action=QueueActionType.OPEN, tab=QueueTab.REVIEW, page=10_000, news_id=big)
    assert len(data.pack().encode()) <= CALLBACK_DATA_LIMIT
    for button in markup_buttons:
        assert len((button.callback_data or "").encode()) <= CALLBACK_DATA_LIMIT


async def test_queue_command() -> None:
    session = await _feed(_db_with(2), message_update(ADMIN, "/queue"))
    [sent] = session.sent()
    assert sent.text.startswith("<b>Очередь публикации</b> · 2 постов")


async def test_page_callback_edits_list() -> None:
    session = await _feed(_db_with(7), _cb(QueueActionType.PAGE, page=1))
    [edit] = [r for r in session.requests if isinstance(r, EditMessageText)]
    assert "страница 2 из 2" in (edit.text or "")

    db = _db_with(1)
    db.add(9, 0.5, status=S.PENDING_REVIEW)
    session = await _feed(db, _cb(QueueActionType.PAGE, tab=QueueTab.REVIEW))
    [edit] = [r for r in session.requests if isinstance(r, EditMessageText)]
    assert (edit.text or "").startswith("<b>На модерации</b>")


async def test_open_sends_preview_with_back_button() -> None:
    db = _db_with(2)
    session = await _feed(db, _cb(QueueActionType.OPEN, page=0, news_id=2))
    [photo] = [r for r in session.requests if isinstance(r, SendPhoto)]
    assert photo.photo == "https://cdn.example/a.jpg"
    assert "<b>Заголовок 2</b>" in (photo.caption or "")
    assert isinstance(photo.reply_markup, InlineKeyboardMarkup)
    [(publish_label, publish), (label, action)] = _buttons(photo.reply_markup)
    assert publish_label == "🚀 Опубликовать сейчас"
    assert (publish.action, publish.news_id, publish.tab) == ("publish", 2, QueueTab.QUEUE)
    assert label == "◀️ К списку"
    assert (action.action, action.news_id, action.tab) == ("back", 2, QueueTab.QUEUE)


async def test_open_review_post_sends_note_first() -> None:
    db = Db()
    db.add(1, 0.5, status=S.PENDING_REVIEW, review_note="Не найдены: 45%", image_url=None)
    session = await _feed(db, _cb(QueueActionType.OPEN, tab=QueueTab.REVIEW, news_id=1))
    sent = [r for r in session.requests if isinstance(r, SendMessage)]
    assert sent[0].text == f"{REVIEW_NOTE_PREFIX} Не найдены: 45%"
    assert "<b>Заголовок 1</b>" in sent[1].text


@pytest.mark.parametrize(
    ("status", "tab"),
    [(S.PUBLISHED, QueueTab.QUEUE), (S.APPROVED, QueueTab.REVIEW), (S.REJECTED, QueueTab.REVIEW)],
)
async def test_open_post_no_longer_in_queue(status: NewsStatus, tab: QueueTab) -> None:
    db = Db()
    db.add(1, 0.5, status=status)
    session = await _feed(db, _cb(QueueActionType.OPEN, tab=tab, news_id=1))
    [answer] = [r for r in session.requests if isinstance(r, AnswerCallbackQuery)]
    assert answer.text == texts.QUEUE_NOT_IN_QUEUE.format(status=status.value)
    assert answer.show_alert is True
    assert not [r for r in session.requests if isinstance(r, SendPhoto | SendMessage)]


async def test_open_deleted_post() -> None:
    session = await _feed(Db(), _cb(QueueActionType.OPEN, news_id=5))
    [answer] = [r for r in session.requests if isinstance(r, AnswerCallbackQuery)]
    assert answer.text == texts.QUEUE_NOT_IN_QUEUE.format(status=texts.QUEUE_DELETED)


async def test_back_sends_list_again() -> None:
    session = await _feed(_db_with(7), _cb(QueueActionType.BACK, page=1, news_id=6))
    [sent] = session.sent()
    assert "страница 2 из 2" in sent.text
    assert not [r for r in session.requests if isinstance(r, EditMessageText)]


def test_real_components_fit_protocols() -> None:
    from app.db.repositories.misc import SettingsRepository
    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
    from app.db.repositories.sources import SourceRepository

    base = _deps(Db())
    deps = QueueDeps(
        session_scope=base.session_scope,
        news_repo=NewsRepository,
        post_repo=ProcessedPostRepository,
        publication_repo=PublicationRepository,
        settings_repo=SettingsRepository,
        source_repo=SourceRepository,
        preview=base.preview,
        publisher_cfg=CFG,
        channel_id=CHANNEL,
        tz=TZ,
        page_size=PAGE_SIZE,
        title_chars=bot_config().queue_title_chars,
        max_per_source=MAX_PER_SOURCE,
        clock=base.clock,
        manual=ManualPublishDeps(publish=MagicMock(), news_repo=NewsRepository),
    )
    assert deps.page_size == PAGE_SIZE


def _deps_with_limit(db: Db, limit: int) -> QueueDeps:
    return replace(_deps(db), max_per_source=limit)


async def test_source_load_shown_in_line() -> None:
    db = Db()
    db.add(1, 0.9)
    db.add(2, 0.8)
    text, _ = await render_page(_deps_with_limit(db, 4), QueueTab.QUEUE, 0, NOW)
    assert all("Пример 2/4" in line for line in _items(text))


async def test_full_source_marked_with_lock() -> None:
    db = Db()
    for news_id in range(1, 5):
        db.add(news_id, 0.9)
    text, _ = await render_page(_deps_with_limit(db, 4), QueueTab.QUEUE, 0, NOW)
    assert all("Пример 🔒4/4" in line for line in _items(text))


async def test_no_load_when_limit_disabled() -> None:
    db = Db()
    db.add(1, 0.9)
    text, _ = await render_page(_deps_with_limit(db, 0), QueueTab.QUEUE, 0, NOW)
    [line] = _items(text)
    assert "/0" not in line and "🔒" not in line
    assert "Пример ·" in line


async def test_review_tab_has_no_load() -> None:
    db = Db()
    db.add(1, 0.9, status=S.PENDING_REVIEW)
    text, _ = await render_page(_deps_with_limit(db, 4), QueueTab.REVIEW, 0, NOW)
    [line] = _items(text)
    assert "/4" not in line
