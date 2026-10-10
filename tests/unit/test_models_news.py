from sqlalchemy import BigInteger, DateTime, Enum, Table
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base
from app.db.models import NewsItem, Source
from app.domain.enums import ModerationMode, NewsStatus, SourceType

SOURCE_COLUMNS = {
    "id",
    "type",
    "url",
    "name",
    "interval_min",
    "enabled",
    "moderation_mode",
    "selectors",
    "fetch_state",
    "last_checked_at",
    "last_item_at",
    "error_count",
    "created_at",
}
NEWS_COLUMNS = {
    "id",
    "source_id",
    "external_id",
    "url",
    "canonical_url",
    "title",
    "raw_text",
    "clean_text",
    "image_url",
    "language",
    "published_at",
    "content_hash",
    "simhash",
    "status",
    "reject_reason",
    "cluster_id",
    "raw_meta",
    "created_at",
    "updated_at",
}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def test_tables_registered_in_metadata() -> None:
    assert Source.__table__ is _table("sources")
    assert NewsItem.__table__ is _table("news_items")


def test_source_columns() -> None:
    assert set(_table("sources").columns.keys()) == SOURCE_COLUMNS


def test_news_item_columns() -> None:
    assert set(_table("news_items").columns.keys()) == NEWS_COLUMNS


def test_all_datetimes_are_timezone_aware() -> None:
    for name in ("sources", "news_items"):
        for column in _table(name).columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone, f"{name}.{column.name}"


def test_news_status_values() -> None:
    assert [s.value for s in NewsStatus] == [
        "new",
        "duplicate",
        "filtered",
        "normalized",
        "processed",
        "pending_review",
        "approved",
        "rejected",
        "scheduled",
        "published",
        "failed",
    ]
    assert [s.value for s in SourceType] == ["rss", "web", "tg"]
    assert [m.value for m in ModerationMode] == ["auto", "review"]


def test_enums_stored_as_strings_by_value() -> None:
    columns = [
        _table("sources").c.type,
        _table("sources").c.moderation_mode,
        _table("news_items").c.status,
    ]
    for column in columns:
        assert isinstance(column.type, Enum)
        assert column.type.native_enum is False
    status_type = _table("news_items").c.status.type
    assert isinstance(status_type, Enum)
    assert status_type.enums == [s.value for s in NewsStatus]


def test_source_id_cascades_on_delete() -> None:
    [fk] = _table("news_items").c.source_id.foreign_keys
    assert fk.column.table.name == "sources"
    assert fk.ondelete == "CASCADE"
    assert _table("news_items").c.source_id.nullable is False


def test_cluster_id_self_reference_nullable() -> None:
    column = _table("news_items").c.cluster_id
    [fk] = column.foreign_keys
    assert fk.column.table.name == "news_items"
    assert column.nullable is True


def test_source_url_unique() -> None:
    assert _table("sources").c.url.unique is True


def test_simhash_is_signed_bigint() -> None:
    assert isinstance(_table("news_items").c.simhash.type, BigInteger)


def test_jsonb_columns() -> None:
    assert isinstance(_table("sources").c.selectors.type, JSONB)
    assert _table("sources").c.selectors.nullable is True
    assert isinstance(_table("sources").c.fetch_state.type, JSONB)
    assert _table("sources").c.fetch_state.server_default is not None
    assert isinstance(_table("news_items").c.raw_meta.type, JSONB)


def test_updated_at_refreshes_on_update() -> None:
    column = _table("news_items").c.updated_at
    assert column.onupdate is not None
    assert column.server_default is not None


def test_news_items_indexes() -> None:
    indexes = {
        str(index.name): ([c.name for c in index.columns], index.unique)
        for index in _table("news_items").indexes
    }
    assert indexes["uq_news_items_source_id_external_id"] == (["source_id", "external_id"], True)
    assert indexes["ix_news_items_canonical_url"] == (["canonical_url"], False)
    assert indexes["ix_news_items_content_hash"] == (["content_hash"], False)
    assert indexes["ix_news_items_status_created_at"] == (["status", "created_at"], False)
    assert indexes["ix_news_items_cluster_id"] == (["cluster_id"], False)
