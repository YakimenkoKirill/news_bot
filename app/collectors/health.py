"""Отслеживание здоровья источников: серии ошибок и отсутствие новостей."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog

from app.config import CollectorsConfig
from app.db.models import Source
from app.domain.alerts import AlertEvent, AlertKind, AlertSink

log = structlog.get_logger(__name__)


class SourceHealthRepo(Protocol):
    """Нужная трекеру часть SourceRepository."""

    async def register_error(self, source_id: int) -> int:
        """Увеличивает счётчик ошибок и возвращает новое значение."""
        raise NotImplementedError

    async def reset_errors(self, source_id: int) -> bool:
        """Обнуляет счётчик ошибок."""
        raise NotImplementedError

    async def mark_item_seen(self, source_id: int, at: datetime) -> bool:
        """Сдвигает last_item_at."""
        raise NotImplementedError


class SourceHealthTracker:
    """Ведёт счётчики ошибок источников и отправляет алерты по порогам.

    source_down отправляется, когда счётчик ошибок подряд достигает
    max_consecutive_errors, — один раз за серию; успех сбрасывает серию.
    source_stale отправляется один раз, пока у источника не появится новость.
    """

    def __init__(
        self,
        repo: SourceHealthRepo,
        sink: AlertSink,
        config: CollectorsConfig,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._sink = sink
        self._max_errors = config.max_consecutive_errors
        self._stale_after = timedelta(hours=config.stale_after_h)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stale_alerted: dict[int, datetime] = {}

    async def on_success(self, source: Source, items_count: int, now: datetime) -> None:
        """Сбрасывает серию ошибок и сдвигает last_item_at, если пришли новости."""
        await self._repo.reset_errors(source.id)
        if items_count > 0:
            await self._repo.mark_item_seen(source.id, now)
            self._stale_alerted.pop(source.id, None)

    async def on_error(self, source: Source, error: BaseException) -> int:
        """Засчитывает ошибку источника.

        Args:
            source: Источник.
            error: Возникшая ошибка.

        Returns:
            Новое число ошибок подряд.
        """
        count = await self._repo.register_error(source.id)
        log.warning("source.error", source_id=source.id, error_count=count, error=repr(error))
        if count == self._max_errors:
            await self._sink.emit(
                AlertEvent(
                    kind=AlertKind.SOURCE_DOWN,
                    key=f"{AlertKind.SOURCE_DOWN.value}:{source.id}",
                    message=(
                        f"Источник «{source.name}» ({source.url}) не отвечает: "
                        f"{count} ошибок подряд. Последняя: {error}"
                    ),
                    created_at=self._clock(),
                )
            )
        return count

    async def check_stale(self, sources: Iterable[Source], now: datetime) -> list[Source]:
        """Отправляет source_stale для включённых источников без новостей дольше порога.

        Для источника без last_item_at отсчёт идёт от created_at.

        Returns:
            Источники, по которым отправлен алерт в этом вызове.
        """
        alerted: list[Source] = []
        for source in sources:
            reference = source.last_item_at or source.created_at
            if not source.enabled or reference is None or now - reference < self._stale_after:
                self._stale_alerted.pop(source.id, None)
                continue
            if self._stale_alerted.get(source.id) == reference:
                continue
            hours = int((now - reference) / timedelta(hours=1))
            await self._sink.emit(
                AlertEvent(
                    kind=AlertKind.SOURCE_STALE,
                    key=f"{AlertKind.SOURCE_STALE.value}:{source.id}",
                    message=f"Источник «{source.name}» ({source.url}) молчит {hours} ч.",
                    created_at=now,
                )
            )
            self._stale_alerted[source.id] = reference
            alerted.append(source)
        return alerted
