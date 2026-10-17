import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

import app.db.models  # noqa: F401  регистрирует модели в Base.metadata
from app.db.base import Base

pytestmark = pytest.mark.integration

T = TypeVar("T")


def _with_connection(url: str, fn: Callable[[Connection], T]) -> T:
    async def run() -> T:
        engine = create_async_engine(url, poolclass=pool.NullPool)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(fn)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _differences(connection: Connection) -> list[Any]:
    return list(compare_metadata(MigrationContext.configure(connection), Base.metadata))


def test_migrations_match_models(alembic_config: Config, test_database_url: str) -> None:
    """Новый столбец или индекс в модели без миграции ломает этот тест.

    Та же проверка стоит в CI отдельной целью (make check-migrations),
    поэтому расхождение не доедет до боевой БД.
    """
    command.upgrade(alembic_config, "head")
    assert _with_connection(test_database_url, _differences) == []


def test_downgrade_to_base_and_back(alembic_config: Config, test_database_url: str) -> None:
    """Схема полностью откатывается и собирается заново."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    assert not set(Base.metadata.tables) & set(
        _with_connection(test_database_url, lambda conn: conn.dialect.get_table_names(conn))
    )

    command.upgrade(alembic_config, "head")
    head = ScriptDirectory.from_config(alembic_config).get_current_head()
    revision = _with_connection(
        test_database_url, lambda conn: MigrationContext.configure(conn).get_current_revision()
    )
    assert revision == head
    assert _with_connection(test_database_url, _differences) == []
