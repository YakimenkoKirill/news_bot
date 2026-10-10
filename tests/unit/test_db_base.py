import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, MetaData, String, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from app.config import CONFIGS_DIR, Settings, load_settings
from app.db.base import (
    NAMING_CONVENTION,
    Base,
    create_engine,
    create_session_factory,
    session_scope,
)

ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/news",
    "TELEGRAM_BOT_TOKEN": "token",
    "TELEGRAM_CHANNEL_ID": "-100",
    "TELEGRAM_ADMIN_IDS": "1",
    "TELETHON_API_ID": "1",
    "TELETHON_API_HASH": "hash",
    "DEEPSEEK_API_KEY": "key",
}


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    configs = tmp_path / "configs"
    configs.mkdir()
    shutil.copy(CONFIGS_DIR / "base.yaml", configs / "base.yaml")
    (configs / "dev.yaml").write_text(
        "db:\n  pool_size: 7\n  max_overflow: 3\n  pool_pre_ping: true\n  echo: false\n",
        encoding="utf-8",
    )
    return load_settings(configs, "dev")


def test_base_uses_naming_convention() -> None:
    assert Base.metadata.naming_convention == NAMING_CONVENTION


def test_naming_convention_gives_deterministic_names() -> None:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table("parents", metadata, Column("id", Integer, primary_key=True))
    children = Table(
        "children",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("parents.id")),
        Column("code", String(10), unique=True),
        Column("title", String(10), index=True),
        CheckConstraint("id > 0", name="positive_id"),
    )
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    ddl = str(CreateTable(children).compile(dialect=dialect))
    assert "CONSTRAINT pk_children PRIMARY KEY" in ddl
    assert "CONSTRAINT fk_children_parent_id_parents FOREIGN KEY" in ddl
    assert "CONSTRAINT uq_children_code UNIQUE" in ddl
    assert "CONSTRAINT ck_children_positive_id CHECK" in ddl
    [index] = children.indexes
    assert "INDEX ix_children_title" in str(CreateIndex(index).compile(dialect=dialect))


async def test_engine_pool_params_from_config(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        pool: Any = engine.pool
        assert pool.size() == 7
        assert pool._max_overflow == 3
        assert pool._pre_ping is True
        assert engine.echo is False
        assert engine.url.drivername == "postgresql+asyncpg"
    finally:
        await engine.dispose()


async def test_session_factory_disables_expire_on_commit(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        assert factory.kw["expire_on_commit"] is False
        assert factory.class_ is AsyncSession
    finally:
        await engine.dispose()


def _mock_factory(session: AsyncMock) -> async_sessionmaker[AsyncSession]:
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    factory: async_sessionmaker[AsyncSession] = MagicMock(return_value=context)
    return factory


async def test_session_scope_commits_on_success() -> None:
    session = AsyncMock()
    async with session_scope(_mock_factory(session)) as opened:
        assert opened is session
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


async def test_session_scope_rolls_back_and_reraises() -> None:
    session = AsyncMock()
    error = ValueError("boom")
    with pytest.raises(ValueError) as caught:
        async with session_scope(_mock_factory(session)):
            raise error
    assert caught.value is error
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


async def test_session_scope_rolls_back_when_commit_fails() -> None:
    session = AsyncMock()
    session.commit.side_effect = RuntimeError("commit failed")
    with pytest.raises(RuntimeError, match="commit failed"):
        async with session_scope(_mock_factory(session)):
            pass
    session.rollback.assert_awaited_once()
