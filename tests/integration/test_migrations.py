import asyncio
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, pool
from sqlalchemy.ext.asyncio import create_async_engine

import app.db.models  # noqa: F401  регистрирует модели в Base.metadata
from app.db.base import Base

pytestmark = pytest.mark.integration

T = TypeVar("T")
APP_TABLES = set(Base.metadata.tables)


def _with_connection(url: str, fn: Callable[[Connection], T]) -> T:
    async def run() -> T:
        engine = create_async_engine(url, poolclass=pool.NullPool)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(fn)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _table_names(url: str) -> set[str]:
    return set(_with_connection(url, lambda conn: inspect(conn).get_table_names()))


def _current_revision(url: str) -> str | None:
    def revision(conn: Connection) -> str | None:
        return MigrationContext.configure(conn).get_current_revision()

    return _with_connection(url, revision)


@pytest.fixture
def clean_db(alembic_config: Config) -> Iterator[Config]:
    command.downgrade(alembic_config, "base")
    yield alembic_config
    command.downgrade(alembic_config, "base")


def test_upgrade_creates_schema_downgrade_removes_it(
    clean_db: Config, test_database_url: str
) -> None:
    assert not APP_TABLES & _table_names(test_database_url)
    command.upgrade(clean_db, "head")
    assert _table_names(test_database_url) >= APP_TABLES
    command.downgrade(clean_db, "base")
    assert not APP_TABLES & _table_names(test_database_url)
    assert _current_revision(test_database_url) is None


def test_repeated_upgrade_is_noop(clean_db: Config, test_database_url: str) -> None:
    head = ScriptDirectory.from_config(clean_db).get_current_head()
    command.upgrade(clean_db, "head")
    command.upgrade(clean_db, "head")
    assert _current_revision(test_database_url) == head == "0004"


def test_news_items_indexes(clean_db: Config, test_database_url: str) -> None:
    command.upgrade(clean_db, "head")
    indexes = _with_connection(
        test_database_url, lambda conn: inspect(conn).get_indexes("news_items")
    )
    found = {str(index["name"]): (index["column_names"], index["unique"]) for index in indexes}
    assert found["uq_news_items_source_id_external_id"] == (["source_id", "external_id"], True)
    assert found["ix_news_items_canonical_url"] == (["canonical_url"], False)
    assert found["ix_news_items_content_hash"] == (["content_hash"], False)
    assert found["ix_news_items_status_created_at"] == (["status", "created_at"], False)
    assert found["ix_news_items_cluster_id"] == (["cluster_id"], False)


def test_incidents_table_added_in_0002(clean_db: Config, test_database_url: str) -> None:
    command.upgrade(clean_db, "head")
    assert "incidents" in _table_names(test_database_url)
    indexes = _with_connection(
        test_database_url, lambda conn: inspect(conn).get_indexes("incidents")
    )
    found = {str(index["name"]): index for index in indexes}
    unique_key = found["uq_incidents_open_key"]
    assert (unique_key["column_names"], unique_key["unique"]) == (["key"], True)
    assert "resolved_at IS NULL" in str(unique_key.get("dialect_options", {}))
    assert found["ix_incidents_resolved_last_seen"]["column_names"] == [
        "resolved_at",
        "last_seen_at",
    ]

    command.downgrade(clean_db, "0001")
    assert "incidents" not in _table_names(test_database_url)
    assert _current_revision(test_database_url) == "0001"


def test_publication_initiator_added_in_0003(clean_db: Config, test_database_url: str) -> None:
    command.upgrade(clean_db, "head")

    def columns(url: str) -> set[str]:
        found = _with_connection(url, lambda conn: inspect(conn).get_columns("publications"))
        return {str(column["name"]) for column in found}

    before_downgrade = columns(test_database_url)
    assert "initiated_by" in before_downgrade

    command.downgrade(clean_db, "0002")
    after = columns(test_database_url)
    assert "initiated_by" not in after
    assert after == before_downgrade - {"initiated_by"}
    assert _current_revision(test_database_url) == "0002"


def test_bot_screens_added_in_0004(clean_db: Config, test_database_url: str) -> None:
    command.upgrade(clean_db, "head")
    assert "bot_screens" in _table_names(test_database_url)
    columns = {
        str(column["name"])
        for column in _with_connection(
            test_database_url, lambda conn: inspect(conn).get_columns("bot_screens")
        )
    }
    assert columns == {"chat_id", "screen_token", "message_ids", "updated_at"}

    command.downgrade(clean_db, "0003")
    assert "bot_screens" not in _table_names(test_database_url)
    assert _current_revision(test_database_url) == "0003"


def test_autogenerate_finds_no_differences(clean_db: Config, test_database_url: str) -> None:
    command.upgrade(clean_db, "head")

    def diff(conn: Connection) -> list[Any]:
        return list(compare_metadata(MigrationContext.configure(conn), Base.metadata))

    assert _with_connection(test_database_url, diff) == []
