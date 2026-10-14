from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.alerts import AlertService
from app.config import AlertsConfig
from app.db.base import session_scope
from app.db.models import Incident, NewsItem, ProcessedPost, Source
from app.db.repositories.incidents import IncidentRepository
from app.db.repositories.news import NewsRepository
from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
from app.domain.alerts import AlertEvent, AlertKind
from app.domain.enums import NewsStatus, SourceType
from app.pipeline.retry import RetryDeps, RetryOutcome, retry_failed
from app.scheduler import JOB_COLLECT, JobRunner
from tests.config_helpers import base_section

pytestmark = pytest.mark.integration

S = NewsStatus
ADMIN = 111
CHANNEL = -1001234567890
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
ALERTS = AlertsConfig(**base_section("alerts"))
SILENT = frozenset(ALERTS.silent_kinds)


class Bot:
    """Бот Telegram: запоминает тексты сообщений админам."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs["text"])


class Env:
    """AlertService с журналом на реальной БД."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.bot = Bot()
        self.clock = 0.0
        self.service = AlertService(
            self.bot,
            [ADMIN],
            ALERTS,
            clock=lambda: self.clock,
            store=self,
            silent_kinds=SILENT,
        )

    def scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(self.factory)

    async def record(self, event: AlertEvent) -> Incident:
        async with self.scope() as session:
            return await IncidentRepository(session).record(event, ALERTS.max_message_chars)

    async def incidents(self, open_only: bool = False) -> list[Incident]:
        async with self.scope() as session:
            return list(await IncidentRepository(session).list(open_only, 100, 0))

    async def resolve_all(self, at: datetime) -> int:
        async with self.scope() as session:
            return await IncidentRepository(session).resolve_all(ADMIN, at)


def _event(
    kind: AlertKind = AlertKind.JOB_FAILED,
    key: str = "job_failed:collect",
    message: str = "Задача collect упала",
    created_at: datetime = NOW,
) -> AlertEvent:
    return AlertEvent(kind=kind, key=key, message=message, created_at=created_at)


async def test_job_failures_make_one_incident(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)

    async def broken(now: datetime) -> None:
        raise RuntimeError("БД недоступна")

    runner = JobRunner(env.service, failures_before_alert=3, clock=lambda: NOW)
    for _ in range(3):
        await runner.run(JOB_COLLECT, broken)

    [incident] = await env.incidents()
    assert incident.kind == AlertKind.JOB_FAILED.value
    assert incident.count == 1
    assert incident.resolved_at is None
    assert len(env.bot.sent) == 1
    assert env.bot.sent[0].endswith("Подробнее: /errors")


async def test_repeat_within_throttle_grows_count_without_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    await env.service.emit(_event())
    await env.service.emit(_event(created_at=NOW + timedelta(minutes=1)))

    [incident] = await env.incidents()
    assert incident.count == 2
    assert incident.last_seen_at == NOW + timedelta(minutes=1)
    assert len(env.bot.sent) == 1

    env.clock += ALERTS.throttle_min * 60
    await env.service.emit(_event(created_at=NOW + timedelta(hours=1)))
    assert len(env.bot.sent) == 2
    assert env.bot.sent[-1].endswith("Повторов: 3 · /errors")


async def test_resolved_incident_reopens_on_next_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    await env.service.emit(_event())
    assert await env.resolve_all(NOW) == 1

    await env.service.emit(_event(created_at=NOW + timedelta(hours=2)))
    incidents = await env.incidents()
    assert len(incidents) == 2
    assert [i.count for i in incidents] == [1, 1]
    assert len(await env.incidents(open_only=True)) == 1


async def test_silent_kind_recorded_without_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    await env.service.emit(_event(AlertKind.AI_ITEM_FAILED, key="ai_item_failed:5"))

    [incident] = await env.incidents()
    assert incident.kind == AlertKind.AI_ITEM_FAILED.value
    assert env.bot.sent == []


async def test_purge_removes_only_old_resolved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    await env.service.emit(_event(key="old"))
    await env.service.emit(_event(key="recent"))
    await env.service.emit(_event(key="open"))

    async with env.scope() as session:
        repo = IncidentRepository(session)
        rows = {i.key: i.id for i in await repo.list(False, 100, 0)}
        await repo.resolve(rows["old"], ADMIN, NOW - timedelta(days=40))
        await repo.resolve(rows["recent"], ADMIN, NOW - timedelta(days=1))

    cutoff = NOW - timedelta(days=ALERTS.retention_days)
    async with env.scope() as session:
        assert await IncidentRepository(session).purge_resolved(cutoff) == 1
    assert {i.key for i in await env.incidents()} == {"recent", "open"}


async def test_failed_publication_is_retryable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    env = Env(session_factory)
    async with env.scope() as session:
        source = Source(type=SourceType.RSS, url="https://r.example/rss", name="R", interval_min=10)
        session.add(source)
        await session.flush()
        news = NewsItem(
            source_id=source.id,
            external_id="1",
            url="https://r.example/1",
            title="Упавшая новость",
            raw_text="текст",
            status=S.FAILED,
            reject_reason="publish_error",
            published_at=NOW,
        )
        session.add(news)
        await session.flush()
        session.add(
            ProcessedPost(
                news_item_id=news.id,
                ai_title="Заголовок",
                ai_summary="Пересказ новости.",
                hashtags=[],
                relevance_score=0.9,
                prompt_version="v3",
                prompt_tokens=1,
                completion_tokens=1,
                cost=Decimal("0.000001"),
            )
        )
        news_id = news.id

    await env.service.emit(
        _event(AlertKind.PUBLISH_ERROR, key=f"publish_error:{news_id}", message="Telegram отклонил")
    )
    [incident] = await env.incidents()
    assert incident.kind == AlertKind.PUBLISH_ERROR.value

    async with env.scope() as session:
        rows = await NewsRepository(session).list_failed(10, 0)
    assert [(n.id, has_post) for n, has_post in rows] == [(news_id, True)]

    result = await retry_failed(
        news_id,
        RetryDeps(
            session_scope=env.scope,
            news_repo=NewsRepository,
            post_repo=ProcessedPostRepository,
        ),
    )
    assert result.outcome is RetryOutcome.TO_QUEUE
    async with env.scope() as session:
        item = await NewsRepository(session).get(news_id)
        assert item is not None and item.status is S.APPROVED
        assert await PublicationRepository(session).last_published_at(CHANNEL) is None
        assert await session.get(Incident, incident.id) is not None
