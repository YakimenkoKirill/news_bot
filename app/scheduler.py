"""Планировщик фоновых задач пайплайна (APScheduler)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import SchedulerConfig
from app.domain.alerts import AlertEvent, AlertKind, AlertSink
from app.logging import bind_context

log = structlog.get_logger(__name__)

Job = Callable[[datetime], Awaitable[Any]]

JOB_COLLECT = "collect"
JOB_NORMALIZE = "normalize"
JOB_AI = "ai"
JOB_PUBLISH = "publish"
JOB_STALE = "check_stale"
JOB_REVIEW = "resend_due"
JOB_HEARTBEAT = "heartbeat"
JOB_PURGE = "purge_incidents"


@dataclass(frozen=True)
class SchedulerDeps:
    """Задачи пайплайна и получатель алертов.

    Каждая задача — асинхронная функция от текущего момента (UTC).

    Attributes:
        collect: run_collection_cycle.
        normalize: process_new_batch.
        ai: process_normalized_batch.
        publish: publish_due.
        check_stale: проверка молчащих источников.
        resend_due: повторная отправка отложенных постов на модерацию.
        heartbeat: запись heartbeat_at в настройки.
        purge_incidents: удаление старых закрытых инцидентов журнала.
        sink: Получатель алертов job_failed.
        clock: Источник текущего времени.
    """

    collect: Job
    normalize: Job
    ai: Job
    publish: Job
    check_stale: Job
    resend_due: Job
    heartbeat: Job
    purge_incidents: Job
    sink: AlertSink
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


class JobRunner:
    """Обёртка задач: логирует старт и итог, ловит исключения, алертит серии ошибок."""

    def __init__(
        self, sink: AlertSink, failures_before_alert: int, clock: Callable[[], datetime]
    ) -> None:
        self._sink = sink
        self._threshold = failures_before_alert
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._running = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def wait_idle(self) -> None:
        """Ждёт завершения всех выполняющихся задач."""
        await self._idle.wait()

    def failures(self, job_id: str) -> int:
        """Число ошибок задачи подряд."""
        return self._failures.get(job_id, 0)

    async def run(self, job_id: str, job: Job) -> None:
        """Выполняет задачу; исключение не выходит наружу и не останавливает планировщик.

        После failures_before_alert ошибок подряд отправляется алерт job_failed
        (один раз за серию); успешный запуск сбрасывает счётчик.
        """
        now = self._clock()
        self._running += 1
        self._idle.clear()
        try:
            with bind_context(job=job_id):
                log.info("job.started")
                try:
                    result = await job(now)
                except Exception as exc:
                    count = self._failures.get(job_id, 0) + 1
                    self._failures[job_id] = count
                    log.exception("job.failed", failures=count)
                    if count == self._threshold:
                        await self._alert(job_id, count, exc, now)
                    return
                self._failures[job_id] = 0
                log.info("job.done", result=repr(result))
        finally:
            self._running -= 1
            if self._running == 0:
                self._idle.set()

    async def _alert(self, job_id: str, count: int, error: Exception, now: datetime) -> None:
        try:
            await self._sink.emit(
                AlertEvent(
                    kind=AlertKind.JOB_FAILED,
                    key=f"{AlertKind.JOB_FAILED.value}:{job_id}",
                    message=(
                        f"Задача «{job_id}» упала {count} раз подряд. Последняя ошибка: {error!r}"
                    ),
                    created_at=now,
                )
            )
        except Exception:
            log.exception("job.alert_failed")


def build_scheduler(
    deps: SchedulerDeps, cfg: SchedulerConfig
) -> tuple[AsyncIOScheduler, JobRunner]:
    """Создаёт планировщик со всеми задачами пайплайна.

    Все задачи: max_instances=1 (запуски не перекрываются), coalesce=True
    (пропущенные запуски схлопываются), misfire_grace_time из конфигурации.

    Returns:
        Планировщик (ещё не запущен) и обёртка задач со счётчиками ошибок.
    """
    runner = JobRunner(deps.sink, cfg.failures_before_alert, deps.clock)
    scheduler = AsyncIOScheduler(timezone=UTC)
    jobs: list[tuple[str, Job, IntervalTrigger]] = [
        (JOB_COLLECT, deps.collect, IntervalTrigger(seconds=cfg.collect_every_s)),
        (JOB_NORMALIZE, deps.normalize, IntervalTrigger(seconds=cfg.normalize_every_s)),
        (JOB_AI, deps.ai, IntervalTrigger(seconds=cfg.ai_every_s)),
        (JOB_PUBLISH, deps.publish, IntervalTrigger(seconds=cfg.publish_every_s)),
        (JOB_STALE, deps.check_stale, IntervalTrigger(minutes=cfg.stale_every_min)),
        (JOB_REVIEW, deps.resend_due, IntervalTrigger(minutes=cfg.review_every_min)),
        (JOB_HEARTBEAT, deps.heartbeat, IntervalTrigger(seconds=cfg.heartbeat_every_s)),
        (JOB_PURGE, deps.purge_incidents, IntervalTrigger(hours=cfg.purge_every_h)),
    ]
    for job_id, job, trigger in jobs:
        scheduler.add_job(
            runner.run,
            trigger=trigger,
            args=[job_id, job],
            id=job_id,
            name=job_id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=cfg.misfire_grace_s,
        )
    return scheduler, runner
