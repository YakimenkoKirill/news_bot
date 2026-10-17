from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.config import SchedulerConfig
from app.domain.alerts import AlertEvent, AlertKind
from app.scheduler import (
    JOB_AI,
    JOB_COLLECT,
    JOB_DAILY_REPORT,
    JOB_HEARTBEAT,
    JOB_NORMALIZE,
    JOB_PUBLISH,
    JOB_PURGE,
    JOB_RECOVER,
    JOB_RETENTION,
    JOB_REVIEW,
    JOB_STALE,
    JobRunner,
    SchedulerDeps,
    build_scheduler,
)
from tests.config_helpers import base_section

CFG = SchedulerConfig(**base_section("scheduler"))
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class Sink:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    async def emit(self, event: AlertEvent) -> None:
        self.events.append(event)


async def _ok(now: datetime) -> str:
    return "ok"


def _deps(sink: Sink | None = None) -> SchedulerDeps:
    return SchedulerDeps(
        collect=_ok,
        normalize=_ok,
        ai=_ok,
        publish=_ok,
        check_stale=_ok,
        resend_due=_ok,
        heartbeat=_ok,
        purge_incidents=_ok,
        daily_report=_ok,
        recover_stuck=_ok,
        retention=_ok,
        sink=sink or Sink(),
        clock=lambda: NOW,
    )


def test_jobs_and_intervals_from_config() -> None:
    scheduler, _ = build_scheduler(_deps(), CFG)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    expected = {
        JOB_COLLECT: timedelta(seconds=CFG.collect_every_s),
        JOB_NORMALIZE: timedelta(seconds=CFG.normalize_every_s),
        JOB_AI: timedelta(seconds=CFG.ai_every_s),
        JOB_PUBLISH: timedelta(seconds=CFG.publish_every_s),
        JOB_STALE: timedelta(minutes=CFG.stale_every_min),
        JOB_REVIEW: timedelta(minutes=CFG.review_every_min),
        JOB_HEARTBEAT: timedelta(seconds=CFG.heartbeat_every_s),
        JOB_PURGE: timedelta(hours=CFG.purge_every_h),
        JOB_DAILY_REPORT: timedelta(minutes=CFG.daily_report_every_min),
        JOB_RECOVER: timedelta(minutes=CFG.recover_every_min),
        JOB_RETENTION: timedelta(hours=CFG.retention_every_h),
    }
    assert set(jobs) == set(expected)
    for job_id, interval in expected.items():
        assert jobs[job_id].trigger.interval == interval, job_id


def test_jobs_do_not_overlap() -> None:
    scheduler, _ = build_scheduler(_deps(), CFG)
    for job in scheduler.get_jobs():
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time == CFG.misfire_grace_s


def test_intervals_change_with_config() -> None:
    cfg = SchedulerConfig(**{**base_section("scheduler"), "collect_every_s": 5})
    scheduler, _ = build_scheduler(_deps(), cfg)
    collect = next(job for job in scheduler.get_jobs() if job.id == JOB_COLLECT)
    assert collect.trigger.interval == timedelta(seconds=5)


async def test_runner_passes_now_and_resets_on_success() -> None:
    seen: list[datetime] = []

    async def job(now: datetime) -> None:
        seen.append(now)

    runner = JobRunner(Sink(), CFG.failures_before_alert, lambda: NOW)
    await runner.run("x", job)
    assert seen == [NOW]
    assert runner.failures("x") == 0


async def test_exception_does_not_escape_and_alert_after_series() -> None:
    sink = Sink()
    runner = JobRunner(sink, 3, lambda: NOW)

    async def broken(now: datetime) -> Any:
        raise RuntimeError("db down")

    for _ in range(2):
        await runner.run(JOB_COLLECT, broken)
    assert sink.events == []
    await runner.run(JOB_COLLECT, broken)
    [event] = sink.events
    assert event.kind is AlertKind.JOB_FAILED
    assert event.key == "job_failed:collect"
    assert "db down" in event.message
    for _ in range(3):
        await runner.run(JOB_COLLECT, broken)
    assert len(sink.events) == 1


async def test_success_resets_series() -> None:
    sink = Sink()
    runner = JobRunner(sink, 2, lambda: NOW)

    async def broken(now: datetime) -> Any:
        raise RuntimeError("boom")

    await runner.run("j", broken)
    await runner.run("j", _ok)
    await runner.run("j", broken)
    assert sink.events == []
    await runner.run("j", broken)
    assert len(sink.events) == 1


async def test_alert_failure_does_not_escape() -> None:
    class BrokenSink:
        async def emit(self, event: AlertEvent) -> None:
            raise RuntimeError("telegram down")

    runner = JobRunner(BrokenSink(), 1, lambda: NOW)

    async def broken(now: datetime) -> Any:
        raise RuntimeError("boom")

    await runner.run("j", broken)
    assert runner.failures("j") == 1


@pytest.mark.parametrize("job_id", [JOB_COLLECT, JOB_PUBLISH])
async def test_scheduled_wrapper_runs_job(job_id: str) -> None:
    calls: list[str] = []

    async def record(now: datetime) -> None:
        calls.append(job_id)

    deps = SchedulerDeps(
        collect=record,
        normalize=_ok,
        ai=_ok,
        publish=record,
        check_stale=_ok,
        resend_due=_ok,
        heartbeat=_ok,
        purge_incidents=_ok,
        daily_report=_ok,
        recover_stuck=_ok,
        retention=_ok,
        sink=Sink(),
        clock=lambda: NOW,
    )
    scheduler, _ = build_scheduler(deps, CFG)
    job = next(j for j in scheduler.get_jobs() if j.id == job_id)
    await job.func(*job.args)
    assert calls == [job_id]
