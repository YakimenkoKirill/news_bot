"""Настройки приложения: переменные окружения, .env и configs/*.yaml.

Единственный модуль проекта, который читает os.environ.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

Environment = Literal["dev", "prod"]

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"
ENVIRONMENTS: tuple[Environment, ...] = get_args(Environment)
DEFAULT_ENV: Environment = "dev"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_yaml_data: ContextVar[dict[str, Any] | None] = ContextVar("_yaml_data", default=None)


class AppConfig(BaseModel):
    """Секция app из configs/*.yaml."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"неизвестный часовой пояс: {value}") from exc
        return value


class DbConfig(BaseModel):
    """Секция db: параметры пула соединений."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pool_size: int = Field(ge=1)
    max_overflow: int = Field(ge=0)
    pool_pre_ping: bool
    echo: bool


class CollectorsConfig(BaseModel):
    """Секция collectors: параметры HTTP-запросов сборщиков."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_agent: str = Field(min_length=1)
    http_timeout_s: float = Field(gt=0)
    per_domain_rps: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    robots_ttl_h: float = Field(gt=0)
    min_article_chars: int = Field(ge=1)
    max_articles_per_run: int = Field(ge=1)
    max_consecutive_errors: int = Field(ge=1)
    stale_after_h: float = Field(gt=0)
    max_parallel_sources: int = Field(ge=1)


class TelegramConfig(BaseModel):
    """Секция telegram: работа с Telegram через Telethon."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    flood_wait_extra_s: float = Field(ge=0)
    flood_wait_max_retries: int = Field(ge=0)
    flood_wait_max_s: float = Field(gt=0)
    title_max_chars: int = Field(ge=1)
    album_wait_s: float = Field(gt=0)
    ad_markers: list[str]
    skip_forwards: bool


class _YamlSource(PydanticBaseSettingsSource):
    """Источник настроек из уже слитых yaml-файлов, самый низкий приоритет."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self().get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(_yaml_data.get() or {})


class Settings(BaseSettings):
    """Настройки приложения.

    Приоритет источников: аргументы конструктора, переменные окружения, .env,
    configs/{ENV}.yaml поверх configs/base.yaml.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
    )

    env: Environment = DEFAULT_ENV
    database_url: str
    telegram_bot_token: SecretStr
    telegram_channel_id: int
    telegram_admin_ids: Annotated[list[int], NoDecode] = Field(min_length=1)
    telethon_api_id: int
    telethon_api_hash: SecretStr
    telethon_session_path: Path = Path("data/telethon")
    deepseek_api_key: SecretStr
    deepseek_base_url: str = "https://api.deepseek.com"
    log_level: LogLevel = "INFO"

    app: AppConfig
    db: DbConfig
    collectors: CollectorsConfig
    telegram: TelegramConfig

    @field_validator("telegram_admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивно сливает словари: значения override заменяют значения base.

    Args:
        base: Исходный словарь.
        override: Словарь с переопределениями.

    Returns:
        Новый словарь; вложенные словари сливаются, остальные значения заменяются.
    """
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидался словарь верхнего уровня")
    return data


def _yaml_sections() -> frozenset[str]:
    return frozenset(
        name
        for name, field in Settings.model_fields.items()
        if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel)
    )


def _check_yaml_sections(data: dict[str, Any]) -> None:
    unknown = sorted(set(data) - _yaml_sections())
    if unknown:
        raise ValidationError.from_exception_data(
            "Settings",
            [{"type": "extra_forbidden", "loc": (key,), "input": data[key]} for key in unknown],
        )


def load_settings(configs_dir: Path | None = None, env: str | None = None) -> Settings:
    """Загружает настройки без кэша.

    Args:
        configs_dir: Каталог с base.yaml и {env}.yaml; по умолчанию configs/ проекта.
        env: Имя окружения; если не задано, берётся из переменной ENV (по умолчанию dev).

    Returns:
        Провалидированные настройки.

    Raises:
        ValueError: Неизвестное окружение.
        pydantic.ValidationError: Нет обязательного поля или лишний ключ в yaml.
    """
    env_name = env if env is not None else os.environ.get("ENV", DEFAULT_ENV)
    if env_name not in ENVIRONMENTS:
        raise ValueError(f"неизвестное окружение ENV={env_name!r}, допустимо: {ENVIRONMENTS}")
    directory = configs_dir if configs_dir is not None else CONFIGS_DIR
    data = deep_merge(
        _read_yaml(directory / "base.yaml"), _read_yaml(directory / f"{env_name}.yaml")
    )
    _check_yaml_sections(data)
    token = _yaml_data.set(data)
    try:
        return Settings(env=env_name)
    finally:
        _yaml_data.reset(token)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек приложения."""
    return load_settings()


class TestDatabaseSettings(BaseSettings):
    """Адрес БД для интеграционных тестов; остальные настройки не требуются."""

    __test__ = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    test_database_url: str | None = None


def get_test_database_url() -> str | None:
    """Возвращает TEST_DATABASE_URL из окружения или .env, если он задан."""
    return TestDatabaseSettings().test_database_url or None
