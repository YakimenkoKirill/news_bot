import itertools
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram.methods import AnswerCallbackQuery, EditMessageText

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import moderation
from app.bot.handlers.moderation import ModerationDeps
from app.bot.keyboards import ReviewAction, ReviewActionType
from app.config import PublisherConfig, Settings
from app.db.models import ProcessedPost
from app.db.repositories.news import StaleStatusError
from app.domain.enums import NewsStatus
from app.domain.status import ensure_transition
from app.pipeline.queue_limit import AdmitOutcome, AdmitResult
from app.pipeline.review import ReviewDeps
from tests.bot_helpers import callback_update, make_bot, message_update
from tests.config_helpers import base_section, bot_config

ADMIN = 111
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
SNOOZE = 60.0
CFG = PublisherConfig(**base_section("publisher"))


@dataclass
class News:
    id: int = 1
    source_id: int = 1
    status: NewsStatus = NewsStatus.PENDING_REVIEW
    url: str = "https://src.example/a"
    image_url: str | None = None
    reason: str | None = None


@dataclass
class Source:
    name: str = "Пример"


@dataclass
class Db:
    news: News = field(default_factory=News)
    post: ProcessedPost = field(
        default_factory=lambda: ProcessedPost(
            id=10,
            news_item_id=1,
            ai_title="Заголовок",
            ai_summary="Старый пересказ.",
            hashtags=[],
            relevance_score=0.9,
            prompt_version="v1",
            prompt_tokens=1,
            completion_tokens=1,
            cost=Decimal("0.001"),
        )
    )
    snooze: datetime | None = None
    race: bool = False


def _status(db: Db) -> NewsStatus:
    return db.news.status


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, news_id: int) -> News | None:
        return self.db.news if news_id == self.db.news.id else None

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        if self.db.race:
            self.db.news.status = NewsStatus.REJECTED
        if self.db.news.status != expected:
            raise StaleStatusError(news_id, expected)
        ensure_transition(expected, new)
        self.db.news.status = new
        self.db.news.reason = reason

    async def claim_batch(
        self, status: NewsStatus | Sequence[NewsStatus], limit: int
    ) -> list[News]:
        statuses = [status] if isinstance(status, NewsStatus) else list(status)
        return [self.db.news] if self.db.news.status in statuses else []


class PostRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, post_id: int) -> ProcessedPost | None:
        return self.db.post if post_id == self.db.post.id else None

    async def update_summary(self, post_id: int, summary: str, edited_by: int) -> bool:
        self.db.post.ai_summary = summary
        self.db.post.edited_by = edited_by
        return True

    async def set_snooze(self, post_id: int, until: datetime | None) -> bool:
        self.db.snooze = until
        return True

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        return self.db.post if news_id == self.db.post.news_item_id else None


class SourceRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, source_id: int) -> Source | None:
        return Source()


class Admission:
    """Подменяет постановку в очередь: запоминает вызовы и переводит статус."""

    def __init__(self, db: Db, displaced: int | None = None) -> None:
        self.db = db
        self.displaced = displaced
        self.calls: list[tuple[int, int, NewsStatus, bool]] = []

    async def admit(
        self,
        session: object,
        news_id: int,
        source_id: int,
        expected: NewsStatus,
        *,
        forced: bool = False,
    ) -> AdmitResult:
        self.calls.append((news_id, source_id, expected, forced))
        if self.db.news.status is not expected:
            raise StaleStatusError(news_id, expected)
        self.db.news.status = NewsStatus.APPROVED
        outcome = AdmitOutcome.ADMITTED_DISPLACED if self.displaced else AdmitOutcome.ADMITTED
        return AdmitResult(outcome, NewsStatus.APPROVED, displaced_id=self.displaced)


class Chat:
    def __init__(self, db: Db, admission: Admission | None = None, review: bool = False) -> None:
        @asynccontextmanager
        async def scope() -> AsyncIterator[Db]:
            yield db

        review_deps = (
            ReviewDeps(session_scope=scope, news_repo=NewsRepo, post_repo=PostRepo)
            if review
            else None
        )

        class NoAdmins:
            async def is_admin(self, telegram_id: int) -> bool:
                return False

        deps = BotDeps(
            settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
            admin_repo=NoAdmins(),
            routers=[moderation.create_router()],
            workflow_data={
                "moderation_deps": ModerationDeps(
                    session_scope=scope,
                    news_repo=NewsRepo,
                    post_repo=PostRepo,
                    source_repo=SourceRepo,
                    cfg=CFG,
                    snooze_min=SNOOZE,
                    clock=lambda: NOW,
                    admission=admission,  # type: ignore[arg-type]
                    review=review_deps,
                )
            },
        )
        self.dispatcher = create_dispatcher(deps)
        self.bot, self.session = make_bot()
        self.ids = itertools.count(1)

    async def press(self, action: ReviewActionType, post_id: int = 10) -> None:
        data = ReviewAction(action=action, post_id=post_id).pack()
        await self.dispatcher.feed_update(self.bot, callback_update(ADMIN, data, next(self.ids)))

    async def say(self, text: str) -> list[str]:
        before = len(self.session.sent_texts())
        await self.dispatcher.feed_update(self.bot, message_update(ADMIN, text, next(self.ids)))
        return self.session.sent_texts()[before:]

    def edits(self) -> list[EditMessageText]:
        return [r for r in self.session.requests if isinstance(r, EditMessageText)]

    def answers(self) -> list[AnswerCallbackQuery]:
        return [r for r in self.session.requests if isinstance(r, AnswerCallbackQuery)]


async def test_publish_approves_and_marks_message() -> None:
    db = Db()
    chat = Chat(db)
    await chat.press(ReviewActionType.PUBLISH)
    assert db.news.status is NewsStatus.APPROVED
    [edit] = chat.edits()
    assert edit.reply_markup is None
    assert texts.REVIEW_DECISIONS["publish"] in (edit.text or "")
    assert str(ADMIN) in (edit.text or "")


async def test_reject() -> None:
    db = Db()
    chat = Chat(db)
    await chat.press(ReviewActionType.REJECT)
    assert db.news.status is NewsStatus.REJECTED
    assert db.news.reason == "rejected_by_moderator"
    assert texts.REVIEW_DECISIONS["reject"] in (chat.edits()[0].text or "")


async def test_reject_through_review_module() -> None:
    """Через ReviewDeps отмена даёт тот же текст и статус, что и раньше."""
    db = Db()
    chat = Chat(db, review=True)
    await chat.press(ReviewActionType.REJECT)
    assert db.news.status is NewsStatus.REJECTED
    assert db.news.reason == "rejected_by_moderator"
    assert texts.REVIEW_DECISIONS["reject"] in (chat.edits()[0].text or "")


async def test_reject_through_review_clears_snooze() -> None:
    """Отложенный «Позже» пост после отмены не вернётся на модерацию."""
    db = Db(snooze=NOW + timedelta(minutes=SNOOZE))
    chat = Chat(db, review=True)
    await chat.press(ReviewActionType.REJECT)
    assert db.snooze is None


async def test_reject_through_review_when_already_decided() -> None:
    db = Db()
    chat = Chat(db, review=True)
    await chat.press(ReviewActionType.PUBLISH)
    await chat.press(ReviewActionType.REJECT)
    assert db.news.status is NewsStatus.APPROVED
    assert chat.answers()[-1].text == texts.REVIEW_ALREADY_DONE.format(status="approved")


async def test_publish_through_review_deps_unchanged() -> None:
    """ReviewDeps не влияет на одобрение: оно идёт прежним путём."""
    db = Db()
    chat = Chat(db, review=True)
    await chat.press(ReviewActionType.PUBLISH)
    assert db.news.status is NewsStatus.APPROVED
    assert texts.REVIEW_DECISIONS["publish"] in (chat.edits()[0].text or "")


async def test_later_snoozes() -> None:
    db = Db()
    chat = Chat(db)
    await chat.press(ReviewActionType.LATER)
    assert db.snooze == NOW + timedelta(minutes=SNOOZE)
    assert db.news.status is NewsStatus.PENDING_REVIEW
    assert "Отложено на 60 мин" in (chat.edits()[0].text or "")


async def test_repeated_decision_already_done() -> None:
    db = Db()
    chat = Chat(db)
    await chat.press(ReviewActionType.PUBLISH)
    await chat.press(ReviewActionType.REJECT)
    assert db.news.status is NewsStatus.APPROVED
    last = chat.answers()[-1]
    assert last.text == texts.REVIEW_ALREADY_DONE.format(status="approved")
    assert len(chat.edits()) == 1


async def test_concurrent_decision_stale_status() -> None:
    db = Db(race=True)
    chat = Chat(db)
    await chat.press(ReviewActionType.PUBLISH)
    assert chat.answers()[-1].text == texts.REVIEW_ALREADY_DONE.format(status="rejected")
    assert chat.edits() == []


async def test_unknown_post() -> None:
    chat = Chat(Db())
    await chat.press(ReviewActionType.PUBLISH, post_id=999)
    assert chat.answers()[-1].text == texts.REVIEW_POST_NOT_FOUND


async def test_edit_flow_with_too_long_text() -> None:
    db = Db()
    chat = Chat(db)
    await chat.press(ReviewActionType.EDIT)
    [ask] = chat.session.sent_texts()
    limit = int(ask.split("не длиннее ")[1].split()[0])
    assert 0 < limit < CFG.text_limit
    assert _status(db) is NewsStatus.PENDING_REVIEW

    assert await chat.say("слово " * (limit // 3)) == [
        texts.REVIEW_SUMMARY_TOO_LONG.format(limit=limit)
    ]
    assert await chat.say("   ") == [texts.REVIEW_SUMMARY_EMPTY]
    [done] = await chat.say("Новый короткий пересказ.")
    assert done.startswith(texts.REVIEW_EDITED)
    assert db.post.ai_summary == "Новый короткий пересказ."
    assert db.post.edited_by == ADMIN
    assert _status(db) is NewsStatus.APPROVED


async def test_edit_limit_uses_caption_when_image() -> None:
    db = Db(news=News(image_url="https://cdn.example/a.jpg"))
    chat = Chat(db)
    await chat.press(ReviewActionType.EDIT)
    limit = int(chat.session.sent_texts()[0].split("не длиннее ")[1].split()[0])
    assert limit < CFG.caption_limit


def test_real_repositories_fit_protocols() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository
    from app.db.repositories.sources import SourceRepository

    deps = ModerationDeps(
        session_scope=MagicMock(),
        news_repo=NewsRepository,
        post_repo=ProcessedPostRepository,
        source_repo=SourceRepository,
        cfg=CFG,
        snooze_min=SNOOZE,
    )
    assert deps.snooze_min == SNOOZE


async def test_publish_button_goes_through_admission() -> None:
    db = Db()
    admission = Admission(db)
    chat = Chat(db, admission)
    await chat.press(ReviewActionType.PUBLISH)

    assert admission.calls == [(1, 1, NewsStatus.PENDING_REVIEW, True)]
    assert db.news.status is NewsStatus.APPROVED


async def test_publish_reports_displaced_post() -> None:
    db = Db()
    chat = Chat(db, Admission(db, displaced=42))
    await chat.press(ReviewActionType.PUBLISH)

    [edit] = chat.edits()
    assert texts.REVIEW_DISPLACED.strip(" ·") in (edit.text or "")


async def test_reject_does_not_use_admission() -> None:
    db = Db()
    admission = Admission(db)
    chat = Chat(db, admission)
    await chat.press(ReviewActionType.REJECT)

    assert admission.calls == []
    assert db.news.status is NewsStatus.REJECTED


async def test_publish_without_admission_unchanged() -> None:
    db = Db()
    await Chat(db).press(ReviewActionType.PUBLISH)
    assert db.news.status is NewsStatus.APPROVED
