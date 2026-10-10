"""Бот управления: фабрики бота и диспетчера."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import common
from app.bot.middlewares import AdminChecker, AdminOnlyMiddleware
from app.config import Settings


@dataclass(frozen=True)
class BotDeps:
    """Зависимости бота управления.

    Attributes:
        settings: Настройки приложения.
        admin_repo: Проверка администратора в БД.
        routers: Роутеры с хендлерами (подключаются по порядку перед подсказкой
            на неизвестную команду).
        workflow_data: Зависимости, доступные хендлерам по имени аргумента.
    """

    settings: Settings
    admin_repo: AdminChecker
    routers: Sequence[Router] = ()
    workflow_data: dict[str, Any] = field(default_factory=dict)


def create_bot(settings: Settings) -> Bot:
    """Создаёт бота с parse_mode=HTML по умолчанию."""
    return Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(deps: BotDeps) -> Dispatcher:
    """Создаёт диспетчер: доступ только для админов, роутеры, зависимости.

    AdminOnlyMiddleware ставится внешним для сообщений и callback-запросов, чтобы
    посторонние получали отказ даже на неизвестные команды. Подсказка на
    неизвестную команду подключается последней.
    """
    dispatcher = Dispatcher(**{"settings": deps.settings, **deps.workflow_data})
    middleware = AdminOnlyMiddleware(deps.settings, deps.admin_repo)
    dispatcher.message.outer_middleware(middleware)
    dispatcher.callback_query.outer_middleware(middleware)
    dispatcher.include_router(common.create_router())
    for router in deps.routers:
        dispatcher.include_router(router)
    dispatcher.include_router(common.create_fallback_router())
    return dispatcher
