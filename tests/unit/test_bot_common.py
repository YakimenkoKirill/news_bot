from typing import Any

import pytest

from app.bot import BotDeps, create_bot, create_dispatcher, texts
from app.bot.handlers.common import bot_commands, setup_bot_commands
from app.config import BotConfig, Settings
from tests.bot_helpers import make_bot, message_update

ADMIN = 111
STRANGER = 999


class NoDbAdmins:
    async def is_admin(self, telegram_id: int) -> bool:
        return False


def _settings() -> Settings:
    return Settings.model_construct(
        telegram_admin_ids=[ADMIN], bot=BotConfig(refusal_cooldown_s=60)
    )


def _dispatcher() -> Any:
    return create_dispatcher(BotDeps(settings=_settings(), admin_repo=NoDbAdmins()))


async def _send(text: str, user_id: int = ADMIN) -> list[str]:
    bot, session = make_bot()
    dispatcher = _dispatcher()
    await dispatcher.feed_update(bot, message_update(user_id, text))
    return session.sent_texts()


async def test_start_greets_with_menu() -> None:
    bot, session = make_bot()
    await _dispatcher().feed_update(bot, message_update(ADMIN, "/start"))
    [sent] = session.sent()
    assert sent.text == texts.START
    keyboard = sent.reply_markup
    assert keyboard is not None
    buttons = [b.text for row in keyboard.keyboard for b in row]  # type: ignore[union-attr]
    assert buttons == [f"/{c}" for c in texts.COMMANDS]


async def test_help_lists_all_commands() -> None:
    [reply] = await _send("/help")
    for command, description in texts.COMMANDS.items():
        assert f"/{command} — {description}" in reply


async def test_unknown_command_hint() -> None:
    assert await _send("/nonexistent") == [texts.UNKNOWN_COMMAND]


async def test_plain_text_ignored() -> None:
    assert await _send("просто текст") == []


async def test_stranger_refused_before_handlers() -> None:
    assert await _send("/start", user_id=STRANGER) == [texts.ACCESS_DENIED]
    assert await _send("/nonexistent", user_id=STRANGER) == [texts.ACCESS_DENIED]


async def test_help_matches_menu_commands() -> None:
    menu = {(c.command, c.description) for c in bot_commands()}
    assert menu == set(texts.COMMANDS.items())

    class Bot:
        def __init__(self) -> None:
            self.commands: list[Any] = []

        async def set_my_commands(self, commands: list[Any]) -> None:
            self.commands = commands

    fake = Bot()
    await setup_bot_commands(fake)
    assert [(c.command, c.description) for c in fake.commands] == list(texts.COMMANDS.items())


async def test_dispatcher_can_be_created_twice() -> None:
    _dispatcher()
    _dispatcher()


@pytest.mark.parametrize("routers", [0, 1])
async def test_extra_routers_before_fallback(routers: int) -> None:
    from aiogram import Router
    from aiogram.filters import Command

    extra = Router(name="extra")

    async def cmd_status(message: Any) -> None:
        await message.answer("статус")

    extra.message.register(cmd_status, Command("status"))
    deps = BotDeps(
        settings=_settings(), admin_repo=NoDbAdmins(), routers=[extra] if routers else []
    )
    bot, session = make_bot()
    await create_dispatcher(deps).feed_update(bot, message_update(ADMIN, "/status"))
    expected = ["статус"] if routers else [texts.UNKNOWN_COMMAND]
    assert session.sent_texts() == expected


async def test_create_bot_uses_html_and_hides_token() -> None:
    from pydantic import SecretStr

    settings = Settings.model_construct(telegram_bot_token=SecretStr("123456:SECRET"))
    bot = create_bot(settings)
    try:
        assert bot.default.parse_mode == "HTML"
        assert "SECRET" not in repr(bot)
    finally:
        await bot.session.close()
