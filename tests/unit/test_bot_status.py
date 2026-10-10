from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import status
from app.bot.handlers.status import StatusDeps, build_status
from app.config import BotConfig, Settings
from app.domain.enums import NewsStatus
from app.logging import setup_logging
from tests.bot_helpers import make_bot, message_update

ADMIN = 111
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
MSK = ZoneInfo("Europe/Moscow")


@dataclass
class Src:
    error_count: int


@dataclass
class Db:
    counts: dict[NewsStatus, int] = field(default_factory=lambda: dict.fromkeys(NewsStatus, 0))
    sources: list[Src] = field(default_factory=list)
    spent: Decimal = Decimal("0")
    last: datetime | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    cost_since_arg: list[datetime] = field(default_factory=list)


class Repo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def count_by_status(self) -> dict[NewsStatus, int]:
        return self.db.counts

    async def list_enabled(self, type: Any = None) -> Sequence[Src]:
        return self.db.sources

    async def cost_since(self, dt: datetime) -> Decimal:
        self.db.cost_since_arg.append(dt)
        return self.db.spent

    async def last_published_at(self, channel_id: int) -> datetime | None:
        return self.db.last

    async def get(self, key: str, default: Any = None) -> Any:
        return self.db.settings.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self.db.settings[key] = value


def _deps(db: Db) -> StatusDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    return StatusDeps(
        session_scope=scope,
        news_repo=Repo,
        source_repo=Repo,
        post_repo=Repo,
        publication_repo=Repo,
        settings_repo=Repo,
        daily_budget=Decimal("0.5"),
        channel_id=-100,
        tz=MSK,
        clock=lambda: NOW,
    )


async def _send(db: Db, text: str) -> list[str]:
    settings = Settings.model_construct(
        telegram_admin_ids=[ADMIN], bot=BotConfig(refusal_cooldown_s=60)
    )

    class NoAdmins:
        async def is_admin(self, telegram_id: int) -> bool:
            return False

    deps = BotDeps(
        settings=settings,
        admin_repo=NoAdmins(),
        routers=[status.create_router()],
        workflow_data={"status_deps": _deps(db)},
    )
    bot, session = make_bot()
    await create_dispatcher(deps).feed_update(bot, message_update(ADMIN, text))
    return session.sent_texts()


async def test_status_format() -> None:
    db = Db(
        sources=[Src(0), Src(3), Src(1)],
        spent=Decimal("0.12345"),
        last=datetime(2026, 10, 10, 8, 30, tzinfo=UTC),
    )
    db.counts[NewsStatus.NEW] = 5
    db.counts[NewsStatus.PUBLISHED] = 12
    text = await build_status(_deps(db))
    assert text == (
        "<b>Состояние</b>\n"
        "Пауза: нет\n"
        "Новости по статусам: new — 5, published — 12\n"
        "Проблемных источников: 2\n"
        "Расход DeepSeek за сутки: 0.12 $ из 0.50 $\n"
        "Последняя публикация: 10.10.2026 11:30"
    )
    assert db.cost_since_arg == [datetime(2026, 10, 10, 0, 0, tzinfo=MSK)]


async def test_status_without_publications_and_news() -> None:
    db = Db(settings={"paused": True})
    text = await build_status(_deps(db))
    assert "Пауза: да" in text
    assert "Новости по статусам: нет" in text
    assert "Последняя публикация: нет" in text
    assert "0.00 $ из 0.50 $" in text


async def test_status_command_through_dispatcher() -> None:
    [reply] = await _send(Db(), "/status")
    assert reply.startswith("<b>Состояние</b>")


async def test_pause_and_resume_with_repeats(capsys: pytest.CaptureFixture[str]) -> None:
    db = Db()
    setup_logging("INFO", json_output=True)
    try:
        assert await _send(db, "/pause") == [texts.PAUSED]
        assert db.settings["paused"] is True
        assert await _send(db, "/pause") == [texts.ALREADY_PAUSED]
        assert await _send(db, "/resume") == [texts.RESUMED]
        assert db.settings["paused"] is False
        assert await _send(db, "/resume") == [texts.NOT_PAUSED]
    finally:
        structlog.reset_defaults()
    out = capsys.readouterr().out
    assert '"bot.paused"' in out
    assert '"bot.resumed"' in out
    assert f'"user_id": {ADMIN}' in out


def test_commands_listed_in_help() -> None:
    for command in ("status", "pause", "resume"):
        assert command in texts.COMMANDS


def test_real_repositories_fit_protocols() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.misc import SettingsRepository
    from app.db.repositories.news import NewsRepository
    from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
    from app.db.repositories.sources import SourceRepository

    deps = StatusDeps(
        session_scope=MagicMock(),
        news_repo=NewsRepository,
        source_repo=SourceRepository,
        post_repo=ProcessedPostRepository,
        publication_repo=PublicationRepository,
        settings_repo=SettingsRepository,
        daily_budget=Decimal("1"),
        channel_id=-100,
        tz=MSK,
    )
    assert deps.clock().tzinfo is UTC
