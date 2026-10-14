"""Расчёт времени следующей публикации с учётом интервала и тихих часов."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

QuietWindow = tuple[time, time]


def in_quiet_hours(local: time, window: QuietWindow) -> bool:
    """Попадает ли локальное время в тихие часы [начало, конец).

    Окно может пересекать полночь (например, 23:00–07:00); начало, равное концу,
    означает отсутствие тихих часов.
    """
    start, end = window
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def _quiet_end(local: datetime, window: QuietWindow, tz: ZoneInfo) -> datetime:
    start, end = window
    end_date = local.date()
    if start > end and local.timetz().replace(tzinfo=None) >= start:
        end_date += timedelta(days=1)
    return datetime.combine(end_date, end, tzinfo=tz)


def next_slot(
    now: datetime,
    last_published_at: datetime | None,
    min_interval_min: float,
    quiet_hours: QuietWindow | None,
    tz: str | ZoneInfo,
) -> datetime:
    """Ближайшее время, когда можно публиковать.

    Не раньше now и last_published_at + min_interval_min и не внутри тихих часов
    (заданы в локальном времени tz). Окончание тихих часов уже разрешено.

    Args:
        now: Текущий момент (timezone-aware).
        last_published_at: Время последней публикации или None.
        min_interval_min: Минимальный интервал между постами в минутах.
        quiet_hours: Тихие часы (начало, конец) или None.
        tz: Часовой пояс канала (app.timezone).

    Returns:
        Время публикации в UTC.
    """
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    candidate = now
    if last_published_at is not None:
        candidate = max(candidate, last_published_at + timedelta(minutes=min_interval_min))
    if quiet_hours is not None:
        local = candidate.astimezone(zone)
        if in_quiet_hours(local.time(), quiet_hours):
            candidate = _quiet_end(local, quiet_hours, zone)
    return candidate.astimezone(UTC)


def queue_eta(
    now: datetime,
    last_published_at: datetime | None,
    count: int,
    min_interval_min: float,
    quiet_hours: QuietWindow | None,
    tz: str | ZoneInfo,
) -> list[datetime]:
    """Ожидаемое время выхода первых count постов очереди.

    Первый пост — в ближайший слот, каждый следующий — через интервал после
    предыдущего с учётом тихих часов. Это оценка: новые посты, пауза и сбои
    публикации не учитываются.

    Returns:
        Время выхода в UTC; пустой список при count = 0.
    """
    result: list[datetime] = []
    previous: datetime | None = None
    for _ in range(count):
        if previous is None:
            slot = next_slot(now, last_published_at, min_interval_min, quiet_hours, tz)
        else:
            slot = next_slot(previous, previous, min_interval_min, quiet_hours, tz)
        result.append(slot)
        previous = slot
    return result
