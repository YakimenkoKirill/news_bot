import asyncio
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any, TypeVar

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

import app.db.models  # noqa: F401  регистрирует модели в Base.metadata
from app.db.base import Base
from app.pipeline.dedup import simhash_bands, to_db

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
    assert _current_revision(test_database_url) == head == "0008"


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


def test_ai_requests_added_in_0005(clean_db: Config, test_database_url: str) -> None:
    command.upgrade(clean_db, "head")
    assert "ai_requests" in _table_names(test_database_url)
    columns = {
        str(column["name"]): column
        for column in _with_connection(
            test_database_url, lambda conn: inspect(conn).get_columns("ai_requests")
        )
    }
    assert set(columns) == {
        "id",
        "news_item_id",
        "kind",
        "model",
        "prompt_tokens",
        "cached_tokens",
        "completion_tokens",
        "cost",
        "ok",
        "created_at",
    }
    assert columns["news_item_id"]["nullable"] is True
    indexes = _with_connection(
        test_database_url, lambda conn: inspect(conn).get_indexes("ai_requests")
    )
    found = {str(index["name"]): index["column_names"] for index in indexes}
    assert found["ix_ai_requests_created_at"] == ["created_at"]

    command.downgrade(clean_db, "0004")
    assert "ai_requests" not in _table_names(test_database_url)
    assert _current_revision(test_database_url) == "0004"


def test_0005_copies_history_from_processed_posts(clean_db: Config, test_database_url: str) -> None:
    """История до миграции видна в отчёте: каждый пересказ превращается в запрос."""
    command.upgrade(clean_db, "0004")

    def seed(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO sources (type, url, name, interval_min) "
                "VALUES ('rss', 'https://m.example/rss', 'M', 10)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO news_items (source_id, external_id, url, title, raw_text, status) "
                "SELECT id, '1', 'https://m.example/1', 't', 'r', 'processed' FROM sources"
            )
        )
        conn.execute(
            text(
                "INSERT INTO processed_posts (news_item_id, ai_title, ai_summary, "
                "relevance_score, prompt_version, prompt_tokens, completion_tokens, cost, "
                "created_at) SELECT id, 't', 's', 0.9, 'v1', 120, 40, 0.00042, "
                "'2026-10-10T09:00:00+00:00' FROM news_items"
            )
        )
        conn.commit()

    _with_connection(test_database_url, seed)
    command.upgrade(clean_db, "head")

    def rows(conn: Connection) -> list[Any]:
        return list(
            conn.execute(
                text(
                    "SELECT news_item_id, kind, prompt_tokens, cached_tokens, "
                    "completion_tokens, cost, ok, created_at FROM ai_requests"
                )
            ).all()
        )

    [row] = _with_connection(test_database_url, rows)
    assert row.kind == "summary"
    assert (row.prompt_tokens, row.cached_tokens, row.completion_tokens) == (120, 0, 40)
    assert row.cost == Decimal("0.000420")
    assert row.ok is True
    assert row.created_at.isoformat() == "2026-10-10T09:00:00+00:00"
    assert row.news_item_id is not None


def test_simhash_bands_added_in_0006(clean_db: Config, test_database_url: str) -> None:
    command.upgrade(clean_db, "head")
    columns = {
        str(column["name"])
        for column in _with_connection(
            test_database_url, lambda conn: inspect(conn).get_columns("news_items")
        )
    }
    assert {"simhash_b0", "simhash_b1", "simhash_b2", "simhash_b3"} <= columns
    indexes = _with_connection(
        test_database_url, lambda conn: inspect(conn).get_indexes("news_items")
    )
    found = {str(index["name"]): index["column_names"] for index in indexes}
    for band in range(4):
        assert found[f"ix_news_items_simhash_b{band}_created_at"] == [
            f"simhash_b{band}",
            "created_at",
        ]

    command.downgrade(clean_db, "0005")
    after = {
        str(column["name"])
        for column in _with_connection(
            test_database_url, lambda conn: inspect(conn).get_columns("news_items")
        )
    }
    assert not {"simhash_b0", "simhash_b3"} & after
    assert _current_revision(test_database_url) == "0005"


def test_0006_fills_bands_of_existing_rows(clean_db: Config, test_database_url: str) -> None:
    """Части считаются и для отрицательных simhash: биты те же, что у беззнакового."""
    command.upgrade(clean_db, "0005")
    unsigned = 0xABCD_1234_5678_9ABC
    negative = 0xFFFF_0000_1234_5678
    assert to_db(negative) < 0

    def seed(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO sources (type, url, name, interval_min) "
                "VALUES ('rss', 'https://b.example/rss', 'B', 10)"
            )
        )
        for index, value in enumerate((unsigned, negative, None)):
            conn.execute(
                text(
                    "INSERT INTO news_items (source_id, external_id, url, title, raw_text, "
                    "status, simhash) SELECT id, :ext, :url, 't', 'r', 'new', :simhash "
                    "FROM sources"
                ).bindparams(
                    ext=str(index),
                    url=f"https://b.example/{index}",
                    simhash=to_db(value) if value is not None else None,
                )
            )
        conn.commit()

    _with_connection(test_database_url, seed)
    command.upgrade(clean_db, "head")

    def rows(conn: Connection) -> list[Any]:
        return list(
            conn.execute(
                text(
                    "SELECT simhash, simhash_b0, simhash_b1, simhash_b2, simhash_b3 "
                    "FROM news_items ORDER BY external_id"
                )
            ).all()
        )

    first, second, third = _with_connection(test_database_url, rows)
    assert (first.simhash_b0, first.simhash_b1, first.simhash_b2, first.simhash_b3) == (
        simhash_bands(unsigned)
    )
    assert (second.simhash_b0, second.simhash_b1, second.simhash_b2, second.simhash_b3) == (
        simhash_bands(negative)
    )
    assert second.simhash < 0
    assert third.simhash is None
    assert third.simhash_b0 is None


REPORT_INDEXES = {
    "publications": "ix_publications_channel_published",
    "processed_posts": "ix_processed_posts_created_at",
    "news_items": "ix_news_items_created_at",
}


def _indexes(url: str, table: str) -> dict[str, list[str]]:
    found = _with_connection(url, lambda conn: inspect(conn).get_indexes(table))
    return {
        str(index["name"]): [str(column) for column in index["column_names"]] for index in found
    }


def test_report_indexes_added_in_0007(clean_db: Config, test_database_url: str) -> None:
    command.upgrade(clean_db, "head")
    assert _indexes(test_database_url, "publications")["ix_publications_channel_published"] == [
        "channel_id",
        "published_at",
    ]
    assert _indexes(test_database_url, "processed_posts")["ix_processed_posts_created_at"] == [
        "created_at"
    ]
    news = _indexes(test_database_url, "news_items")
    assert news["ix_news_items_created_at"] == ["created_at"]
    assert news["ix_news_items_source_status"] == ["source_id", "status"]

    command.downgrade(clean_db, "0006")
    after_news = _indexes(test_database_url, "news_items")
    assert "ix_news_items_created_at" not in after_news
    assert "ix_news_items_source_status" not in after_news
    assert after_news["ix_news_items_status_created_at"] == ["status", "created_at"]
    assert "ix_news_items_simhash_b0_created_at" in after_news
    assert "ix_publications_channel_published" not in _indexes(test_database_url, "publications")
    assert "ix_processed_posts_created_at" not in _indexes(test_database_url, "processed_posts")
    assert "ix_ai_requests_created_at" in _indexes(test_database_url, "ai_requests")
    assert _current_revision(test_database_url) == "0006"


def test_repeated_0007_upgrade_is_noop(clean_db: Config, test_database_url: str) -> None:
    """Индексы создаются с IF NOT EXISTS: повторный upgrade не падает."""
    command.upgrade(clean_db, "head")
    command.downgrade(clean_db, "0006")
    command.upgrade(clean_db, "head")
    news = _indexes(test_database_url, "news_items")
    assert {"ix_news_items_created_at", "ix_news_items_source_status"} <= set(news)


def test_autogenerate_finds_no_differences(clean_db: Config, test_database_url: str) -> None:
    command.upgrade(clean_db, "head")

    def diff(conn: Connection) -> list[Any]:
        return list(compare_metadata(MigrationContext.configure(conn), Base.metadata))

    assert _with_connection(test_database_url, diff) == []
