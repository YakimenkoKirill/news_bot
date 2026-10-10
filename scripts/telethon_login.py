"""Разовый интерактивный вход в Telegram для создания файла сессии Telethon.

Запуск из корня репозитория (после make install и заполнения .env):

    .venv/bin/python scripts/telethon_login.py

Скрипт спросит номер телефона, код из Telegram и, если включён, пароль 2FA.
Файл сессии сохраняется по пути TELETHON_SESSION_PATH и даёт полный доступ
к аккаунту: не коммитьте его и храните как секрет.
"""

from __future__ import annotations

import asyncio

from app.collectors.telegram.client import build_client
from app.config import get_settings


async def login() -> None:
    """Выполняет вход и печатает, под каким аккаунтом создана сессия."""
    settings = get_settings()
    client = build_client(settings)
    await client.start()
    try:
        me = await client.get_me()
        username = f"@{me.username}" if me.username else "без username"
        session_file = f"{settings.telethon_session_path}.session"
        print(f"Сессия сохранена: {session_file} ({username}, id {me.id})")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(login())
