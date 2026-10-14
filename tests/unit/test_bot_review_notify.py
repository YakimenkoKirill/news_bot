from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from app.bot.keyboards import ReviewAction, ReviewActionType
from app.bot.moderation import REVIEW_NOTE_PREFIX, BotReviewNotifier, ReviewNotifierDeps
from app.config import PublisherConfig
from app.db.models import ProcessedPost
from app.publisher.formatter import build_post
from app.publisher.limits import fit_post
from app.publisher.media import ImageCheck
from tests.config_helpers import base_section

CFG = PublisherConfig(**base_section("publisher"))
ADMINS = [111, 222, 333]
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
METHOD = SendMessage(chat_id=1, text="x")


@dataclass
class News:
    id: int
    source_id: int = 1
    url: str = "https://src.example/a"
    image_url: str | None = "https://cdn.example/a.jpg"


@dataclass
class Source:
    name: str = "Пример"


def _post(
    post_id: int, review_note: str | None = None, news_id: int | None = None
) -> ProcessedPost:
    return ProcessedPost(
        id=post_id,
        news_item_id=news_id or post_id,
        ai_title=f"Заголовок {post_id}",
        ai_summary="Пересказ новости.",
        hashtags=["#тег"],
        relevance_score=0.9,
        prompt_version="v1",
        prompt_tokens=1,
        completion_tokens=1,
        cost=Decimal("0.001"),
        review_note=review_note,
    )


@dataclass
class Db:
    news: dict[int, News] = field(default_factory=dict)
    due: list[ProcessedPost] = field(default_factory=list)
    snoozes: dict[int, datetime | None] = field(default_factory=dict)


class Repo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def get(self, item_id: int) -> Any:
        return self.db.news.get(item_id) if item_id in self.db.news else Source()

    async def list_due_reviews(self, now: datetime) -> Sequence[ProcessedPost]:
        return self.db.due

    async def set_snooze(self, post_id: int, until: datetime | None) -> bool:
        self.db.snoozes[post_id] = until
        return True


class NewsRepo(Repo):
    async def get(self, item_id: int) -> News | None:
        return self.db.news.get(item_id)


class SourceRepo(Repo):
    async def get(self, item_id: int) -> Source | None:
        return Source()


class FakeBot:
    def __init__(
        self, failing: dict[int, Exception] | None = None, photo_error: bool = False
    ) -> None:
        self.failing = failing or {}
        self.photo_error = photo_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _maybe_fail(self, chat_id: int) -> None:
        if chat_id in self.failing:
            raise self.failing[chat_id]

    async def send_message(self, **kwargs: Any) -> None:
        self._maybe_fail(kwargs["chat_id"])
        self.calls.append(("message", kwargs))

    async def send_photo(self, **kwargs: Any) -> None:
        self._maybe_fail(kwargs["chat_id"])
        if self.photo_error:
            raise TelegramBadRequest(method=METHOD, message="wrong file identifier")
        self.calls.append(("photo", kwargs))


def _notifier(db: Db, bot: FakeBot, image_ok: bool = True) -> BotReviewNotifier:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    async def admins() -> Sequence[int]:
        return ADMINS

    async def check(url: str | None) -> ImageCheck:
        return ImageCheck(ok=image_ok, reason=None if image_ok else "bad_type:svg")

    deps = ReviewNotifierDeps(
        session_scope=scope,
        news_repo=NewsRepo,
        source_repo=SourceRepo,
        post_repo=Repo,
        admin_ids=admins,
        check_image=check,
        cfg=CFG,
    )
    return BotReviewNotifier(bot, deps)


def _buttons(kwargs: dict[str, Any]) -> list[ReviewAction]:
    markup = kwargs["reply_markup"]
    return [ReviewAction.unpack(b.callback_data) for row in markup.inline_keyboard for b in row]


async def test_sent_to_all_admins_exactly_as_in_channel() -> None:
    db = Db(news={1: News(1)})
    bot = FakeBot()
    await _notifier(db, bot).notify(_post(1))
    assert [kwargs["chat_id"] for _, kwargs in bot.calls] == ADMINS
    expected = fit_post(build_post(_post(1), News(1), Source(), CFG), has_photo=True, cfg=CFG)
    for kind, kwargs in bot.calls:
        assert kind == "photo"
        assert kwargs["caption"] == expected.html
        assert kwargs["photo"] == "https://cdn.example/a.jpg"
    actions = [a.action for a in _buttons(bot.calls[0][1])]
    assert actions == [
        ReviewActionType.PUBLISH,
        ReviewActionType.EDIT,
        ReviewActionType.REJECT,
        ReviewActionType.LATER,
    ]
    assert {a.post_id for a in _buttons(bot.calls[0][1])} == {1}


async def test_review_note_sent_before_preview() -> None:
    db = Db(news={1: News(1)})
    bot = FakeBot()
    await _notifier(db, bot).notify(_post(1, review_note="Не найдены в источнике: 45%"))
    first_kind, first = bot.calls[0]
    assert first_kind == "message"
    assert first["text"] == f"{REVIEW_NOTE_PREFIX} Не найдены в источнике: 45%"
    assert bot.calls[1][0] == "photo"
    assert len(bot.calls) == 2 * len(ADMINS)


async def test_text_preview_without_photo() -> None:
    db = Db(news={1: News(1, image_url=None)})
    bot = FakeBot()
    await _notifier(db, bot, image_ok=False).notify(_post(1))
    kinds = {kind for kind, _ in bot.calls}
    assert kinds == {"message"}
    assert "link_preview_options" not in bot.calls[0][1]


async def test_rejected_photo_falls_back_to_text() -> None:
    db = Db(news={1: News(1)})
    bot = FakeBot(photo_error=True)
    await _notifier(db, bot).notify(_post(1))
    assert [kind for kind, _ in bot.calls] == ["message"] * len(ADMINS)


async def test_error_for_one_admin_does_not_block_others() -> None:
    db = Db(news={1: News(1)})
    forbidden = TelegramForbiddenError(method=METHOD, message="bot was blocked by the user")
    bot = FakeBot(failing={222: forbidden})
    await _notifier(db, bot).notify(_post(1))
    assert [kwargs["chat_id"] for _, kwargs in bot.calls] == [111, 333]


async def test_missing_news_skips_quietly() -> None:
    bot = FakeBot()
    await _notifier(Db(), bot).notify(_post(1))
    assert bot.calls == []


async def test_resend_due_sends_and_clears_snooze() -> None:
    db = Db(news={1: News(1), 2: News(2)}, due=[_post(1), _post(2)])
    db.snoozes = {1: NOW - timedelta(minutes=1), 2: NOW - timedelta(hours=1)}
    bot = FakeBot()
    assert await _notifier(db, bot).resend_due(NOW) == 2
    assert len(bot.calls) == 2 * len(ADMINS)
    assert db.snoozes == {1: None, 2: None}


def test_real_components_fit_protocols() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository
    from app.db.repositories.sources import SourceRepository
    from app.pipeline.stage_ai import ReviewNotifier

    async def admins() -> Sequence[int]:
        return []

    async def check(url: str | None) -> ImageCheck:
        return ImageCheck(ok=False)

    deps = ReviewNotifierDeps(
        session_scope=MagicMock(),
        news_repo=NewsRepository,
        source_repo=SourceRepository,
        post_repo=ProcessedPostRepository,
        admin_ids=admins,
        check_image=check,
        cfg=CFG,
    )
    notifier: ReviewNotifier = BotReviewNotifier(MagicMock(), deps)
    assert notifier is not None
