"""Конфигурация для тестов: секции из configs/base.yaml с переопределениями."""

from typing import Any

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
