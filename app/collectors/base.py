"""Базовый интерфейс сборщиков: сырая новость, абстрактный сборщик и ошибки."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.db.models import Source

HTTP_SCHEMES = frozenset({"http", "https"})


def _check_http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme.lower() not in HTTP_SCHEMES or not parts.netloc:
        raise ValueError(f"ожидался http(s)-адрес, получено: {value!r}")
    return value


class RawItem(BaseModel):
    """Новость в том виде, в каком её вернул сборщик, до нормализации."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source_id: int = Field(ge=1)
    external_id: str = Field(min_length=1)
    url: str
    title: str
    text: str
    image_url: str | None = None
    published_at: AwareDatetime | None = None
    raw_meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _check_http_url(value)

    @field_validator("image_url")
    @classmethod
    def _check_image_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        return _check_http_url(value)


class CollectorError(Exception):
    """Базовая ошибка сборщика."""

    retryable: bool = False


class TemporaryCollectorError(CollectorError):
    """Временная ошибка (таймаут, 5xx, лимит запросов): повтор имеет смысл."""

    retryable = True


class PermanentCollectorError(CollectorError):
    """Постоянная ошибка (404, запрет robots.txt, неверный формат): повтор бесполезен."""

    retryable = False


class BaseCollector(ABC):
    """Сборщик новостей одного типа источников.

    Реализации бросают наружу только исключения иерархии CollectorError.
    """

    @abstractmethod
    async def fetch(self, source: Source) -> list[RawItem]:
        """Забирает новые материалы источника.

        Args:
            source: Источник с его настройками и fetch_state.

        Returns:
            Сырые новости источника.

        Raises:
            TemporaryCollectorError: Временная ошибка, источник можно опросить позже.
            PermanentCollectorError: Источник недоступен или настроен неверно.
        """
