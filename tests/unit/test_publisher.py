from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from app.config import PublisherConfig
from app.publisher.limits import FittedPost
from app.publisher.publisher import Publisher, PublishError
from tests.config_helpers import base_section

CFG = PublisherConfig(**base_section("publisher"))
CHANNEL = -1001234567890
POST_HTML = '<b>Заголовок</b>\n\nТекст\n\n<a href="https://src.example/1">Источник: S</a>'
IMG = "https://cdn.example/1.jpg"
LINK = "https://src.example/1"
METHOD = SendMessage(chat_id=CHANNEL, text="x")


def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=METHOD, message="Flood control", retry_after=seconds)


def _bad_request(message: str = "Bad Request: wrong file identifier") -> TelegramBadRequest:
    return TelegramBadRequest(method=METHOD, message=message)


class FakeBot:
    def __init__(self, photo: list[Any] | None = None, text: list[Any] | None = None) -> None:
        self.photo_outcomes = list(photo or [])
        self.text_outcomes = list(text or [])
        self.photo_calls: list[dict[str, Any]] = []
        self.text_calls: list[dict[str, Any]] = []

    @staticmethod
    def _next(outcomes: list[Any], default_id: int) -> SimpleNamespace:
        outcome = outcomes.pop(0) if outcomes else default_id
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(message_id=outcome)

    async def send_photo(self, **kwargs: Any) -> SimpleNamespace:
        self.photo_calls.append(kwargs)
        return self._next(self.photo_outcomes, 101)

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        self.text_calls.append(kwargs)
        return self._next(self.text_outcomes, 202)


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _publisher(bot: FakeBot, sleep: FakeSleep | None = None) -> Publisher:
    return Publisher(bot, CHANNEL, CFG, sleep=sleep or FakeSleep())


async def test_photo_post() -> None:
    bot = FakeBot()
    message_id = await _publisher(bot).send(FittedPost(POST_HTML, use_photo=True), IMG, LINK)
    assert message_id == 101
    [call] = bot.photo_calls
    assert call == {
        "chat_id": CHANNEL,
        "photo": IMG,
        "caption": POST_HTML,
        "parse_mode": ParseMode.HTML,
    }
    assert bot.text_calls == []


async def test_text_post_with_preview() -> None:
    bot = FakeBot()
    message_id = await _publisher(bot).send(FittedPost(POST_HTML, use_photo=False), IMG, LINK)
    assert message_id == 202
    [call] = bot.text_calls
    assert call["text"] == POST_HTML
    assert call["parse_mode"] == ParseMode.HTML
    preview = call["link_preview_options"]
    assert preview.url == LINK
    assert preview.prefer_large_media is True
    assert bot.photo_calls == []


async def test_text_without_link_has_no_preview_options() -> None:
    bot = FakeBot()
    await _publisher(bot).send(FittedPost(POST_HTML, use_photo=False), None)
    assert bot.text_calls[0]["link_preview_options"] is None


async def test_photo_flag_without_image_sends_text() -> None:
    bot = FakeBot()
    assert await _publisher(bot).send(FittedPost(POST_HTML, use_photo=True), None, LINK) == 202


async def test_retry_after_waits_and_retries() -> None:
    sleep = FakeSleep()
    bot = FakeBot(photo=[_retry_after(7), _retry_after(3), 555])
    message_id = await _publisher(bot, sleep).send(FittedPost(POST_HTML, True), IMG, LINK)
    assert message_id == 555
    assert sleep.calls == [7, 3]


async def test_retry_after_exhausted() -> None:
    sleep = FakeSleep()
    bot = FakeBot(text=[_retry_after(1)] * (CFG.max_retries + 1))
    with pytest.raises(PublishError, match="частоту"):
        await _publisher(bot, sleep).send(FittedPost(POST_HTML, False), None, LINK)
    assert len(bot.text_calls) == CFG.max_retries + 1
    assert len(sleep.calls) == CFG.max_retries


async def test_photo_rejected_falls_back_to_text() -> None:
    bot = FakeBot(photo=[_bad_request()])
    message_id = await _publisher(bot).send(FittedPost(POST_HTML, True), IMG, LINK)
    assert message_id == 202
    assert len(bot.photo_calls) == 1
    assert bot.text_calls[0]["link_preview_options"].url == LINK


async def test_text_error_after_photo_rejection() -> None:
    bot = FakeBot(photo=[_bad_request()], text=[_bad_request("Bad Request: can't parse entities")])
    with pytest.raises(PublishError, match="can't parse entities"):
        await _publisher(bot).send(FittedPost(POST_HTML, True), IMG, LINK)


@pytest.mark.parametrize("use_photo", [True, False])
async def test_other_errors_wrapped(use_photo: bool) -> None:
    forbidden = TelegramForbiddenError(method=METHOD, message="Forbidden: bot is not a member")
    bot = FakeBot(photo=[forbidden], text=[forbidden])
    with pytest.raises(PublishError, match="not a member"):
        await _publisher(bot).send(FittedPost(POST_HTML, use_photo), IMG, LINK)
    assert len(bot.photo_calls) + len(bot.text_calls) == 1


def test_bot_token_not_in_publisher_repr() -> None:
    token = "123456:secret-token"
    bot = SimpleNamespace(token=token)
    publisher = Publisher(bot, CHANNEL, CFG)
    assert token not in repr(publisher)
