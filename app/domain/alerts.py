"""События для алертов администраторам."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class AlertKind(StrEnum):
    """Вид события."""

    SOURCE_DOWN = "source_down"
    SOURCE_STALE = "source_stale"
    AI_DOWN = "ai_down"
    BUDGET_EXCEEDED = "budget_exceeded"
    PUBLISH_ERROR = "publish_error"
    JOB_FAILED = "job_failed"
    AI_ITEM_FAILED = "ai_item_failed"
    STATS_UNAVAILABLE = "stats_unavailable"


class AlertEvent(BaseModel):
    """Событие для алерта.

    Attributes:
        kind: Вид события.
        key: Ключ для подавления повторов (например, source_down:42).
        message: Текст для администратора.
        created_at: Время события (timezone-aware).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AlertKind
    key: str = Field(min_length=1)
    message: str = Field(min_length=1)
    created_at: AwareDatetime


class AlertSink(Protocol):
    """Получатель алертов (бот администраторов, лог и т. п.)."""

    async def emit(self, event: AlertEvent) -> None:
        """Отправляет событие."""
        raise NotImplementedError
