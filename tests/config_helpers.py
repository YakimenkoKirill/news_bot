"""Конфигурация для тестов: секции из configs/base.yaml с переопределениями."""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config import (
    CONFIGS_DIR,
    BotConfig,
    CollectorsConfig,
    NormalizeConfig,
    TelegramConfig,
)


def base_section(name: str) -> dict[str, Any]:
    """Возвращает копию секции configs/base.yaml."""
    data = yaml.safe_load((CONFIGS_DIR / "base.yaml").read_text(encoding="utf-8"))
    section: dict[str, Any] = dict(data[name])
    return section


def collectors_config(**overrides: Any) -> CollectorsConfig:
    """Секция collectors из base.yaml с заменой отдельных значений."""
    return CollectorsConfig(**{**base_section("collectors"), **overrides})


def telegram_config(**overrides: Any) -> TelegramConfig:
    """Секция telegram из base.yaml с заменой отдельных значений."""
    return TelegramConfig(**{**base_section("telegram"), **overrides})


def normalize_config(**overrides: Any) -> NormalizeConfig:
    """Секция normalize из base.yaml с заменой отдельных значений."""
    return NormalizeConfig(**{**base_section("normalize"), **overrides})


def bot_config(**overrides: Any) -> BotConfig:
    """Секция bot из base.yaml с заменой отдельных значений."""
    return BotConfig(**{**base_section("bot"), **overrides})


def configs_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Каталог configs для load_settings в тестах: копия base.yaml и пустые env-файлы.

    Обязательные переменные окружения заполняются заглушками, посторонние —
    снимаются, поэтому настройки читаются одинаково на любой машине.

    Args:
        tmp_path: Временный каталог теста.
        monkeypatch: Фикстура pytest для переменных окружения.

    Returns:
        Путь к созданному каталогу configs.
    """
    monkeypatch.chdir(tmp_path)
    env = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "TELEGRAM_CHANNEL_ID": "-1001234567890",
        "TELEGRAM_ADMIN_IDS": "111",
        "TELETHON_API_ID": "1",
        "TELETHON_API_HASH": "hash",
        "DEEPSEEK_API_KEY": "key",
    }
    for name in (*env, "ENV", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    configs = tmp_path / "configs"
    configs.mkdir()
    shutil.copy(CONFIGS_DIR / "base.yaml", configs / "base.yaml")
    (configs / "dev.yaml").write_text("# пусто\n", encoding="utf-8")
    return configs
