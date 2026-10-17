from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, load_settings
from app.db.models import AiRequest, NewsItem, Setting, Source
from app.domain.enums import NewsStatus, SourceType
from app.health import (
    AI_OK_SETTING,
    HEARTBEAT_SETTING,
    PAUSED_SETTING,
    TG_ALIVE_SETTING,
    evaluate_state,
    heartbeat_value,
    read_state,
)
from tests.config_helpers import configs_copy

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
STARTED = NOW - timedelta(days=1)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    return load_settings(configs_copy(tmp_path, monkeypatch), "dev")


async def _mark(session: AsyncSession, key: str, value: object) -> None:
    """Пишет настройку, перезаписывая уже созданную."""
    stored = await session.get(Setting, key)
    if stored is None:
        session.add(Setting(key=key, value=value))
    else:
        stored.value = value
    await session.flush()


async def _source(session: AsyncSession, source_type: SourceType, enabled: bool = True) -> int:
    source = Source(
        type=source_type,
        url=f"https://h.example/{source_type.value}",
        name=source_type.value,
        interval_min=10,
        enabled=enabled,
    )
    session.add(source)
    await session.flush()
    return source.id


async def _news(
    session: AsyncSession, source_id: int, status: NewsStatus, created_at: datetime
) -> None:
    session.add(
        NewsItem(
            source_id=source_id,
            external_id=f"{status.value}-{created_at.timestamp()}",
            url=f"https://h.example/{created_at.timestamp()}",
            title="t",
            raw_text="r",
            status=status,
            created_at=created_at,
        )
    )
    await session.flush()


async def _healthy_base(session: AsyncSession) -> None:
    await _mark(session, HEARTBEAT_SETTING, heartbeat_value(STARTED, NOW - timedelta(seconds=5)))
    await _mark(session, TG_ALIVE_SETTING, (NOW - timedelta(seconds=5)).isoformat())
    await _mark(session, AI_OK_SETTING, (NOW - timedelta(minutes=1)).isoformat())


async def test_healthy_state_read_from_db(session: AsyncSession, settings: Settings) -> None:
    await _healthy_base(session)
    await _source(session, SourceType.TG)

    state = await read_state(session, settings, NOW)

    assert state.has_tg_sources is True
    assert state.ai_backlog is False
    assert state.paused is False
    assert state.budget_exhausted is False
    assert state.started_at == STARTED
    assert all(not reasons for reasons in evaluate_state(state, NOW, settings.health).values())


async def test_disconnected_telethon_detected(session: AsyncSession, settings: Settings) -> None:
    await _healthy_base(session)
    await _mark(session, TG_ALIVE_SETTING, (NOW - timedelta(hours=2)).isoformat())
    await _source(session, SourceType.TG)

    problems = evaluate_state(await read_state(session, settings, NOW), NOW, settings.health)
    assert problems["telegram"]
    assert problems["ai"] == []


async def test_disabled_tg_source_is_not_checked(session: AsyncSession, settings: Settings) -> None:
    await _healthy_base(session)
    await _mark(session, TG_ALIVE_SETTING, (NOW - timedelta(hours=2)).isoformat())
    await _source(session, SourceType.TG, enabled=False)
    await _source(session, SourceType.RSS)

    state = await read_state(session, settings, NOW)
    assert state.has_tg_sources is False
    assert evaluate_state(state, NOW, settings.health)["telegram"] == []


async def test_backlog_with_silent_model_detected(
    session: AsyncSession, settings: Settings
) -> None:
    await _healthy_base(session)
    await _mark(session, AI_OK_SETTING, (NOW - timedelta(hours=3)).isoformat())
    source_id = await _source(session, SourceType.RSS)
    await _news(session, source_id, NewsStatus.NORMALIZED, NOW - timedelta(hours=2))

    state = await read_state(session, settings, NOW)
    assert state.ai_backlog is True
    problems = evaluate_state(state, NOW, settings.health)
    assert problems["ai"]
    assert problems["telegram"] == []


async def test_fresh_backlog_is_not_a_problem(session: AsyncSession, settings: Settings) -> None:
    """Новости ждут меньше ai_backlog_min — модель ещё не опоздала."""
    await _healthy_base(session)
    await _mark(session, AI_OK_SETTING, (NOW - timedelta(hours=3)).isoformat())
    source_id = await _source(session, SourceType.RSS)
    await _news(session, source_id, NewsStatus.NORMALIZED, NOW - timedelta(minutes=5))

    state = await read_state(session, settings, NOW)
    assert state.ai_backlog is False
    assert evaluate_state(state, NOW, settings.health)["ai"] == []


async def test_pause_hides_model_problem(session: AsyncSession, settings: Settings) -> None:
    await _healthy_base(session)
    await _mark(session, AI_OK_SETTING, (NOW - timedelta(hours=3)).isoformat())
    await _mark(session, PAUSED_SETTING, True)
    source_id = await _source(session, SourceType.RSS)
    await _news(session, source_id, NewsStatus.NORMALIZED, NOW - timedelta(hours=2))

    state = await read_state(session, settings, NOW)
    assert state.paused is True
    assert evaluate_state(state, NOW, settings.health)["ai"] == []


async def test_exhausted_budget_hides_model_problem(
    session: AsyncSession, settings: Settings
) -> None:
    await _healthy_base(session)
    await _mark(session, AI_OK_SETTING, (NOW - timedelta(hours=3)).isoformat())
    source_id = await _source(session, SourceType.RSS)
    await _news(session, source_id, NewsStatus.NORMALIZED, NOW - timedelta(hours=2))
    session.add(
        AiRequest(
            news_item_id=None,
            kind="summary",
            model="m",
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            cost=settings.ai.daily_budget,
            ok=True,
            created_at=NOW - timedelta(hours=1),
        )
    )
    await session.flush()

    state = await read_state(session, settings, NOW)
    assert state.budget_exhausted is True
    assert evaluate_state(state, NOW, settings.health)["ai"] == []


async def test_spend_below_budget_keeps_check(session: AsyncSession, settings: Settings) -> None:
    await _healthy_base(session)
    session.add(
        AiRequest(
            news_item_id=None,
            kind="summary",
            model="m",
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            cost=Decimal("0.000001"),
            ok=True,
            created_at=NOW - timedelta(hours=1),
        )
    )
    await session.flush()

    state = await read_state(session, settings, NOW)
    assert state.budget_exhausted is False


async def test_recovery_makes_state_healthy_again(
    session: AsyncSession, settings: Settings
) -> None:
    """После восстановления отметок проблем больше нет."""
    await _healthy_base(session)
    await _mark(session, TG_ALIVE_SETTING, (NOW - timedelta(hours=2)).isoformat())
    await _source(session, SourceType.TG)
    assert evaluate_state(await read_state(session, settings, NOW), NOW, settings.health)[
        "telegram"
    ]

    later = NOW + timedelta(minutes=1)
    stored = await session.get(Setting, TG_ALIVE_SETTING)
    assert stored is not None
    stored.value = later.isoformat()
    await session.flush()

    problems = evaluate_state(await read_state(session, settings, later), later, settings.health)
    assert problems["telegram"] == []
