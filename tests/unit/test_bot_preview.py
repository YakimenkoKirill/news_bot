from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.preview import PostPreview, PreviewDeps, build_preview, mark_message, send_preview
from app.config import PublisherConfig
from app.db.models import ProcessedPost
from app.publisher.formatter import build_post
from app.publisher.limits import fit_post
from app.publisher.media import ImageCheck
from tests.config_helpers import base_section

CFG = PublisherConfig(**base_section("publisher"))
METHOD = SendMessage(chat_id=1, text="x")
CHAT = 111
KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Опубликовать", callback_data="x")]]
)


@dataclass
class News:
    id: int
    source_id: int = 1
    url: str = "https://src.example/a"
    image_url: str | None = "https://cdn.example/a.jpg"


@dataclass
class Source:
    name: str = "Пример"


def _post(news_id: int = 1) -> ProcessedPost:
    return ProcessedPost(
        id=news_id * 10,
        news_item_id=news_id,
        ai_title="Заголовок",
        ai_summary="Пересказ новости.",
        hashtags=["#тег"],
        relevance_score=0.9,
        prompt_version="v1",
        prompt_tokens=1,
        completion_tokens=1,
        cost=Decimal("0.001"),
    )


def _deps(news: News | None, source: Source | None, image_ok: bool = True) -> PreviewDeps:
    class NewsRepo:
        def __init__(self, session: object) -> None:
            pass

        async def get(self, news_id: int) -> News | None:
            return news

    class SourceRepo:
        def __init__(self, session: object) -> None:
            pass

        async def get(self, source_id: int) -> Source | None:
            return source

    @asynccontextmanager
    async def scope() -> AsyncIterator[None]:
        yield None

    async def check(url: str | None) -> ImageCheck:
        if url is None:
            return ImageCheck(ok=False, reason="no_image")
        return ImageCheck(ok=image_ok, reason=None if image_ok else "bad_type:svg")

    return PreviewDeps(
        session_scope=scope,
        news_repo=NewsRepo,
        source_repo=SourceRepo,
        check_image=check,
        cfg=CFG,
    )


class FakeBot:
    def __init__(self, photo_error: bool = False) -> None:
        self.photo_error = photo_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_message(self, **kwargs: Any) -> str:
        self.calls.append(("message", kwargs))
        return "message-sent"

    async def send_photo(self, **kwargs: Any) -> str:
        if self.photo_error:
            raise TelegramBadRequest(method=METHOD, message="wrong file identifier")
        self.calls.append(("photo", kwargs))
        return "photo-sent"


async def test_build_preview_with_image() -> None:
    news = News(1)
    preview = await build_preview(_post(), _deps(news, Source()))
    expected = fit_post(build_post(_post(), news, Source(), CFG), has_photo=True, cfg=CFG)
    assert preview == PostPreview(fitted=expected, image_url=news.image_url, link=news.url)
    assert preview.fitted.use_photo is True


async def test_build_preview_without_image() -> None:
    news = News(1, image_url=None)
    preview = await build_preview(_post(), _deps(news, Source()))
    assert preview is not None
    assert preview.fitted.use_photo is False
    assert preview.image_url is None


async def test_build_preview_rejected_image_is_text() -> None:
    preview = await build_preview(_post(), _deps(News(1), Source(), image_ok=False))
    assert preview is not None
    assert preview.fitted.use_photo is False


@pytest.mark.parametrize(("news", "source"), [(News(1), None), (None, Source())])
async def test_build_preview_missing_data(news: News | None, source: Source | None) -> None:
    assert await build_preview(_post(), _deps(news, source)) is None


async def _preview(news: News) -> PostPreview:
    preview = await build_preview(_post(), _deps(news, Source()))
    assert preview is not None
    return preview


async def test_send_preview_photo() -> None:
    preview = await _preview(News(1))
    bot = FakeBot()
    sent = await send_preview(bot, CHAT, preview, KEYBOARD)
    assert sent == "photo-sent"
    [(kind, kwargs)] = bot.calls
    assert kind == "photo"
    assert kwargs["chat_id"] == CHAT
    assert kwargs["photo"] == "https://cdn.example/a.jpg"
    assert kwargs["caption"] == preview.fitted.html
    assert kwargs["parse_mode"] is ParseMode.HTML
    assert kwargs["reply_markup"] is KEYBOARD


async def test_send_preview_falls_back_to_text() -> None:
    preview = await _preview(News(1))
    bot = FakeBot(photo_error=True)
    sent = await send_preview(bot, CHAT, preview, KEYBOARD)
    assert sent == "message-sent"
    [(kind, kwargs)] = bot.calls
    assert kind == "message"
    assert kwargs["text"] == preview.fitted.html
    assert "link_preview_options" not in kwargs
    assert kwargs["reply_markup"] is KEYBOARD


async def test_send_preview_text_without_image_and_keyboard() -> None:
    preview = await _preview(News(1, image_url=None))
    bot = FakeBot()
    await send_preview(bot, CHAT, preview, None)
    [(kind, kwargs)] = bot.calls
    assert kind == "message"
    assert kwargs["reply_markup"] is None


class FakeMessage:
    def __init__(self, caption: str | None = None, fail: bool = False) -> None:
        self.caption = caption
        self.html_text = "<b>Пост</b>"
        self.fail = fail
        self.calls: list[tuple[Any, ...]] = []

    async def edit_caption(self, caption: str, reply_markup: Any) -> None:
        self._maybe_fail()
        self.calls.append(("caption", caption, reply_markup))

    async def edit_text(self, text: str, reply_markup: Any) -> None:
        self._maybe_fail()
        self.calls.append(("text", text, reply_markup))

    async def edit_reply_markup(self, reply_markup: Any) -> None:
        self.calls.append(("markup", reply_markup))

    async def answer(self, text: str) -> None:
        self.calls.append(("answer", text))

    def _maybe_fail(self) -> None:
        if self.fail:
            raise TelegramBadRequest(method=METHOD, message="message is not modified")


@dataclass
class Callback:
    message: FakeMessage


async def test_mark_message_photo_edits_caption() -> None:
    message = FakeMessage(caption="Пост")
    await mark_message(Callback(message), "Опубликовано", "Анна")
    assert message.calls == [("caption", "<b>Пост</b>\n\n<b>Опубликовано</b> — Анна", None)]


async def test_mark_message_text_edits_text() -> None:
    message = FakeMessage()
    await mark_message(Callback(message), "Отклонено", "<Анна>")
    assert message.calls == [("text", "<b>Пост</b>\n\n<b>Отклонено</b> — &lt;Анна&gt;", None)]


async def test_mark_message_fallback_on_edit_error() -> None:
    message = FakeMessage(fail=True)
    await mark_message(Callback(message), "Опубликовано", "Анна")
    assert message.calls == [("markup", None), ("answer", "Опубликовано — Анна")]
