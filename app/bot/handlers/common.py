"""Общие команды бота: /start, /help и подсказка на неизвестную команду."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup

from app.bot import texts

MENU_ROW_SIZE = 2


def bot_commands() -> list[BotCommand]:
    """Команды для меню Telegram (совпадают со списком в /help)."""
    return [
        BotCommand(command=command, description=description)
        for command, description in texts.COMMANDS.items()
    ]


def main_menu() -> ReplyKeyboardMarkup:
    """Клавиатура с командами из texts.COMMANDS."""
    buttons = [KeyboardButton(text=f"/{command}") for command in texts.COMMANDS]
    rows = [buttons[i : i + MENU_ROW_SIZE] for i in range(0, len(buttons), MENU_ROW_SIZE)]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def setup_bot_commands(bot: Any) -> None:
    """Регистрирует команды в меню бота."""
    await bot.set_my_commands(bot_commands())


async def cmd_start(message: Any) -> None:
    """Приветствие и меню."""
    await message.answer(texts.START, reply_markup=main_menu())


async def cmd_help(message: Any) -> None:
    """Список команд."""
    await message.answer(texts.help_text())


async def unknown_command(message: Any) -> None:
    """Подсказка на неизвестную команду."""
    await message.answer(texts.UNKNOWN_COMMAND)


def create_router() -> Router:
    """Роутер /start и /help (новый экземпляр на каждый диспетчер)."""
    router = Router(name="common")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    return router


def create_fallback_router() -> Router:
    """Роутер подсказки на неизвестную команду; подключается последним."""
    router = Router(name="fallback")
    router.message.register(unknown_command, F.text.startswith("/"))
    return router
