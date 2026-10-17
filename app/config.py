"""Настройки приложения: переменные окружения, .env и configs/*.yaml.

Единственный модуль проекта, который читает os.environ.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from datetime import datetime, time
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.domain.alerts import AlertKind
from app.publisher.slots import in_quiet_hours

log = structlog.get_logger(__name__)

Environment = Literal["dev", "prod"]

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"
ENVIRONMENTS: tuple[Environment, ...] = get_args(Environment)
DEFAULT_ENV: Environment = "dev"
QUIET_HOURS_FORMAT = "%H:%M"
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
SIMHASH_BANDS = 4
SIMHASH_BAND_BITS = 16
SIMHASH_MAX_DISTANCE = SIMHASH_BANDS - 1
HOURS_IN_DAY = 24
QUIET_HOURS_BOUNDS = 2

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ReportDay = Literal["previous", "current"]

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
    min_interval_min: int = Field(ge=1)


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
    catch_up_limit: int = Field(ge=1)


class NormalizeConfig(BaseModel):
    """Секция normalize: нормализация текста и URL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    drop_query_params: list[str]
    lang_ratio: float = Field(gt=0, le=1)


class FiltersConfig(BaseModel):
    """Секция filters: отсев новостей до обработки моделью."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_age_h: float = Field(gt=0)
    allowed_languages: list[str] = Field(min_length=1)


class DedupConfig(BaseModel):
    """Секция dedup: поиск дублей и кластеров."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    first_paragraph_chars: int = Field(ge=1)
    window_h: float = Field(gt=0)
    simhash_max_distance: int = Field(ge=0)

    @field_validator("simhash_max_distance")
    @classmethod
    def _check_simhash_distance(cls, value: int) -> int:
        if value > SIMHASH_MAX_DISTANCE:
            raise ValueError(
                f"simhash_max_distance: не больше {SIMHASH_MAX_DISTANCE}. Похожие новости "
                f"ищутся по индексу {SIMHASH_BANDS} частей SimHash по {SIMHASH_BAND_BITS} бит: "
                f"при расстоянии до {SIMHASH_MAX_DISTANCE} хотя бы одна часть совпадает точно, "
                "а при большем дубль можно не найти"
            )
        return value


class PipelineConfig(BaseModel):
    """Секция pipeline: общие параметры стадий обработки."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_size: int = Field(ge=1)


class AiConfig(BaseModel):
    """Секция ai: модель и параметры запросов к DeepSeek."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    model: str = Field(min_length=1)
    temperature: float = Field(ge=0, le=2)
    timeout_s: float = Field(gt=0)
    max_concurrency: int = Field(ge=1)
    max_retries: int = Field(ge=0)
    max_input_chars: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    two_stage: bool
    review_foreign: bool
    screen_input_chars: int = Field(ge=1)
    screen_max_tokens: int = Field(ge=1)
    price_input_per_1m: Decimal = Field(ge=0)
    price_input_cached_per_1m: float | None = Field(default=None, ge=0)
    price_output_per_1m: Decimal = Field(ge=0)
    daily_budget: Decimal = Field(gt=0)
    min_score: float = Field(ge=0, le=1)


class ChannelProfile(BaseModel):
    """Секция channel: профиль канала для промпта и проверки ответа модели."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(min_length=1)
    language: str = Field(min_length=1)
    tone: str = Field(min_length=1)
    max_title_chars: int = Field(ge=1)
    max_summary_chars: int = Field(ge=1)
    hashtags_max: int = Field(ge=0)


class PublisherConfig(BaseModel):
    """Секция publisher: публикация в канал."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    caption_limit: int = Field(ge=1)
    text_limit: int = Field(ge=1)
    min_summary_chars: int = Field(ge=1)
    image_types: list[str] = Field(min_length=1)
    image_max_bytes: int = Field(ge=1)
    min_interval_min: float = Field(ge=0)
    max_queued_per_source: int = Field(ge=0)
    stuck_after_min: float = Field(gt=0)
    quiet_hours: list[str]
    max_retries: int = Field(ge=0)
    score_decay_per_h: float = Field(ge=0)
    cluster_bonus: float = Field(ge=0)
    cluster_bonus_max: int = Field(ge=0)
    footer_mark: str
    footer_title: str
    footer_url: str

    @field_validator("footer_url")
    @classmethod
    def _check_footer_url(cls, value: str) -> str:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(f"footer_url: нужен адрес http(s), получено {value!r}")
        return value

    @field_validator("quiet_hours")
    @classmethod
    def _check_quiet_hours(cls, value: list[str]) -> list[str]:
        if not value:
            return value
        if len(value) != QUIET_HOURS_BOUNDS:
            raise ValueError("quiet_hours: пусто или [начало, конец], например ['23:00', '07:00']")
        for item in value:
            try:
                datetime.strptime(item, QUIET_HOURS_FORMAT)
            except ValueError as exc:
                raise ValueError(f"quiet_hours: время {item!r} не в формате ЧЧ:ММ") from exc
        return value

    @property
    def quiet_window(self) -> tuple[time, time] | None:
        """Тихие часы (начало, конец) в локальном времени или None."""
        if not self.quiet_hours:
            return None
        start, end = (
            datetime.strptime(item, QUIET_HOURS_FORMAT).time() for item in self.quiet_hours
        )
        return start, end


class ModerationConfig(BaseModel):
    """Секция moderation: модерация постов администраторами."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snooze_min: float = Field(gt=0)


class AlertsConfig(BaseModel):
    """Секция alerts: алерты администраторам."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    throttle_min: float = Field(ge=0)
    silent_kinds: list[AlertKind]
    max_message_chars: int = Field(ge=1)
    retention_days: int = Field(ge=1)


class SchedulerConfig(BaseModel):
    """Секция scheduler: интервалы фоновых задач."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collect_every_s: float = Field(gt=0)
    normalize_every_s: float = Field(gt=0)
    ai_every_s: float = Field(gt=0)
    publish_every_s: float = Field(gt=0)
    stale_every_min: float = Field(gt=0)
    review_every_min: float = Field(gt=0)
    heartbeat_every_s: float = Field(gt=0)
    purge_every_h: float = Field(gt=0)
    daily_report_every_min: float = Field(gt=0)
    recover_every_min: float = Field(gt=0)
    retention_every_h: float = Field(gt=0)
    stats_every_min: float = Field(gt=0)
    health_every_min: float = Field(gt=0)
    misfire_grace_s: int = Field(ge=1)
    failures_before_alert: int = Field(ge=1)


class DailyReportConfig(BaseModel):
    """Секция daily_report: ежедневный отчёт в боте."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    send_at: str
    report_day: ReportDay
    top_posts: int = Field(ge=1)
    top_sources: int = Field(ge=1)
    source_name_chars: int = Field(ge=1)
    budget_warn_share: float = Field(gt=0, le=1)

    @field_validator("send_at")
    @classmethod
    def _check_send_at(cls, value: str) -> str:
        try:
            datetime.strptime(value, QUIET_HOURS_FORMAT)
        except ValueError as exc:
            raise ValueError(f"send_at: время {value!r} не в формате ЧЧ:ММ") from exc
        return value

    @property
    def send_time(self) -> time:
        """Время отправки отчёта в локальном времени канала."""
        return datetime.strptime(self.send_at, QUIET_HOURS_FORMAT).time()


class RuntimeOptions(BaseModel):
    """Секция runtime: параметры, изменяемые без перезапуска (app/runtime_config.py)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cache_s: float = Field(gt=0)


class StatsConfig(BaseModel):
    """Секция stats: сбор статистики опубликованных постов."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delay_h: float = Field(gt=0)
    max_age_h: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_window(self) -> StatsConfig:
        """Проверяет, что окно сбора статистики не пустое.

        Raises:
            ValueError: max_age_h не больше delay_h.
        """
        if self.max_age_h <= self.delay_h:
            raise ValueError(
                f"stats.max_age_h ({self.max_age_h}) должен быть больше delay_h "
                f"({self.delay_h}): иначе окно сбора статистики пустое"
            )
        return self


class RetentionConfig(BaseModel):
    """Секция retention: очистка текстов старых новостей."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text_days: int = Field(ge=1)
    batch_size: int = Field(ge=1)
    max_run_s: float = Field(gt=0)


class MetricsConfig(BaseModel):
    """Секция metrics: цели MVP для отчёта по тестовому прогону."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_delay_min: float = Field(gt=0)
    max_rejected_share: float = Field(ge=0, le=1)
    max_duplicate_share: float = Field(ge=0, le=1)


class HealthConfig(BaseModel):
    """Секция health: проверка живости для healthcheck контейнера."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_age_s: float = Field(gt=0)
    db_timeout_s: float = Field(gt=0)
    startup_grace_s: float = Field(ge=0)
    tg_beat_s: float = Field(gt=0)
    tg_max_silence_s: float = Field(gt=0)
    ai_backlog_min: float = Field(gt=0)


class BackupConfig(BaseModel):
    """Секция backup: бэкапы БД и сессии Telethon."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    keep_days: int = Field(ge=1)
    dir: Path


class UiConfig(BaseModel):
    """Секция ui: как выглядит чат администратора с ботом."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    single_screen: bool
    delete_user_messages: bool
    delete_batch_size: int = Field(ge=1)


class BotConfig(BaseModel):
    """Секция bot: бот управления для администраторов."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    refusal_cooldown_s: float = Field(ge=0)
    page_size: int = Field(ge=1)
    max_keyword_chars: int = Field(ge=1)
    queue_title_chars: int = Field(ge=1)
    error_preview_chars: int = Field(ge=1)


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
    normalize: NormalizeConfig
    filters: FiltersConfig
    dedup: DedupConfig
    pipeline: PipelineConfig
    ai: AiConfig
    channel: ChannelProfile
    publisher: PublisherConfig
    ui: UiConfig
    bot: BotConfig
    moderation: ModerationConfig
    alerts: AlertsConfig
    scheduler: SchedulerConfig
    runtime: RuntimeOptions
    stats: StatsConfig
    retention: RetentionConfig
    daily_report: DailyReportConfig
    metrics: MetricsConfig
    health: HealthConfig
    backup: BackupConfig

    @field_validator("telegram_admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def _check_retention_covers_dedup(self) -> Settings:
        """Тексты нельзя очищать раньше, чем по ним ищутся дубли.

        Raises:
            ValueError: retention.text_days меньше окна dedup.window_h.
        """
        needed = int(self.dedup.window_h // HOURS_IN_DAY) + 1
        if self.retention.text_days < needed:
            raise ValueError(
                f"retention.text_days = {self.retention.text_days} меньше окна поиска дублей "
                f"dedup.window_h = {self.dedup.window_h} ч: нужно не меньше {needed} дней, "
                "иначе дубли перестанут находиться"
            )
        return self

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


def config_warnings(settings: Settings) -> list[str]:
    """Несогласованности конфигурации, которые не запрещают запуск.

    Args:
        settings: Провалидированные настройки.

    Returns:
        Сообщения для лога; пустой список, если всё согласовано.
    """
    messages: list[str] = []
    report = settings.daily_report
    window = settings.publisher.quiet_window
    if (
        report.enabled
        and report.report_day == "current"
        and (window is None or not in_quiet_hours(report.send_time, window))
    ):
        messages.append(
            f"daily_report.send_at = {report.send_at} вне publisher.quiet_hours: "
            "отчёт за текущие сутки уйдёт до конца публикаций"
        )
    return messages


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
        settings = Settings(env=env_name)
    finally:
        _yaml_data.reset(token)
    for message in config_warnings(settings):
        log.warning("config.inconsistent", message=message)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек приложения."""
    return load_settings()


class TestDatabaseSettings(BaseSettings):
    """Адрес БД для интеграционных тестов; остальные настройки не требуются."""

    __test__ = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    test_database_url: str | None = None
    require_test_db: str | None = None


def get_test_database_url() -> str | None:
    """Возвращает TEST_DATABASE_URL из окружения или .env, если он задан."""
    return TestDatabaseSettings().test_database_url or None


def require_test_db() -> bool:
    """Обязательны ли интеграционные тесты (REQUIRE_TEST_DB).

    В CI переменная выставлена: без доступной тестовой БД тесты должны падать,
    а не пропускаться, иначе опечатка в workflow оставит job зелёным.

    Returns:
        True, если REQUIRE_TEST_DB включает строгий режим.
    """
    value = TestDatabaseSettings().require_test_db
    return str(value).strip().lower() in TRUE_VALUES


def child_process_env(**extra: str) -> dict[str, str]:
    """Окружение для дочернего процесса: текущее окружение плюс extra.

    Нужно, например, чтобы передать PGPASSWORD в pg_dump, не указывая пароль в argv.
    """
    return {**os.environ, **extra}
