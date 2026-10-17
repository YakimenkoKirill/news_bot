from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, load_settings
from app.db.base import session_scope
from app.db.repositories.misc import SettingsRepository
from app.db.repositories.news import NewsRepository, Priority
from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
from app.db.repositories.sources import SourceRepository
from app.pipeline.queue_limit import QUEUE_STATUSES, QueueAdmission
from app.publisher.stage_publish import PublishDeps, publisher_config
from app.runtime_config import OVERRIDES_SETTING, QUEUE_LIMIT_KEY, RuntimeConfig
from tests.config_helpers import configs_copy
from tests.fakes import RecordingSink, image_never_ok

pytestmark = pytest.mark.integration

ADMIN = 111
CHANNEL = -1001234567890


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    return load_settings(configs_copy(tmp_path, monkeypatch), "dev")


class ClockStub:
    """Время для кэша снимка: тест двигает его вручную."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def tick(self, seconds: float) -> None:
        self.value += seconds


def _runtime(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> tuple[RuntimeConfig, ClockStub]:
    def scope() -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(factory)

    clock = ClockStub()
    return RuntimeConfig(scope, SettingsRepository, settings, clock=clock), clock


async def test_override_survives_new_instance(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Значение лежит в settings: его видит и другой экземпляр приложения."""
    first, _ = _runtime(settings, session_factory)
    await first.set("ai.min_score", "0.85", ADMIN)

    second, _ = _runtime(settings, session_factory)
    assert (await second.snapshot()).ai.min_score == 0.85

    async with session_scope(session_factory) as session:
        stored = await SettingsRepository(session).get(OVERRIDES_SETTING)
    assert stored == {"ai.min_score": 0.85}


async def test_reset_removes_value_from_db(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    runtime, _ = _runtime(settings, session_factory)
    await runtime.set("publisher.min_interval_min", "45", ADMIN)
    await runtime.reset("publisher.min_interval_min", ADMIN)

    async with session_scope(session_factory) as session:
        assert await SettingsRepository(session).get(OVERRIDES_SETTING) == {}
    snapshot = await runtime.snapshot()
    assert snapshot.publisher.min_interval_min == settings.publisher.min_interval_min


async def test_change_visible_after_cache_expires(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Другой экземпляр видит правку не позже runtime.cache_s."""
    reader, clock = _runtime(settings, session_factory)
    assert (await reader.snapshot()).ai.min_score == settings.ai.min_score

    writer, _ = _runtime(settings, session_factory)
    await writer.set("ai.min_score", "0.95", ADMIN)

    assert (await reader.snapshot()).ai.min_score == settings.ai.min_score, "кэш ещё живёт"
    clock.tick(settings.runtime.cache_s + 1)
    assert (await reader.snapshot()).ai.min_score == 0.95


async def test_publisher_config_follows_override(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    runtime, _ = _runtime(settings, session_factory)
    await runtime.set("publisher.quiet_hours", "01:00-02:00", ADMIN)
    await runtime.set("publisher.min_interval_min", "90", ADMIN)

    def scope() -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(session_factory)

    deps = PublishDeps(
        session_scope=scope,
        news_repo=NewsRepository,
        post_repo=ProcessedPostRepository,
        source_repo=SourceRepository,
        publication_repo=PublicationRepository,
        settings_repo=SettingsRepository,
        check_image=image_never_ok,
        publisher=_Sender(),
        sink=RecordingSink(),
        cfg=settings.publisher,
        channel_id=CHANNEL,
        tz=settings.app.timezone,
        runtime=runtime,
    )
    cfg = await publisher_config(deps)
    assert cfg.min_interval_min == 90
    assert cfg.quiet_hours == ["01:00", "02:00"]


class _Sender:
    async def send(self, fitted: object, photo: object, link: object = None) -> int:
        raise AssertionError("отправка в этом тесте не нужна")


async def test_admission_limits_follow_override(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    runtime, _ = _runtime(settings, session_factory)
    admission = QueueAdmission(
        news_repo=NewsRepository,
        source_repo=SourceRepository,
        max_per_source=settings.publisher.max_queued_per_source,
        priority=Priority.from_config(settings.publisher),
        runtime=runtime,
    )
    assert await admission.limits() == (
        settings.publisher.max_queued_per_source,
        Priority.from_config(settings.publisher),
    )

    await runtime.set(QUEUE_LIMIT_KEY, "1", ADMIN)
    await runtime.set("publisher.score_decay_per_h", "0", ADMIN)

    max_per_source, priority = await admission.limits()
    assert max_per_source == 1
    assert priority.decay_per_h == 0


async def test_queue_counts_still_work_with_override(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Приоритет из снимка пригоден для запросов очереди."""
    runtime, _ = _runtime(settings, session_factory)
    await runtime.set("publisher.score_decay_per_h", "0.5", ADMIN)
    publisher = (await runtime.snapshot()).publisher

    async with session_scope(session_factory) as session:
        rows = await NewsRepository(session).list_prepared(
            QUEUE_STATUSES, Priority.from_config(publisher), limit=10, offset=0
        )
    assert rows == []
    assert publisher.score_decay_per_h == 0.5
