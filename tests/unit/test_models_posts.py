from sqlalchemy import BigInteger, DateTime, Numeric, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.db.base import Base
from app.db.models import NewsItem, ProcessedPost, Publication
from app.domain.enums import AdminRole, KeywordType, PublicationStatus

EXPECTED_COLUMNS = {
    "processed_posts": {
        "id",
        "news_item_id",
        "ai_title",
        "ai_summary",
        "hashtags",
        "category",
        "relevance_score",
        "prompt_version",
        "prompt_tokens",
        "completion_tokens",
        "cost",
        "review_note",
        "snoozed_until",
        "edited_by",
        "created_at",
    },
    "publications": {
        "id",
        "processed_post_id",
        "channel_id",
        "message_id",
        "published_at",
        "status",
        "error",
        "initiated_by",
    },
    "keywords": {"id", "word", "type"},
    "admins": {"telegram_id", "role", "created_at"},
    "settings": {"key", "value", "updated_at"},
    "incidents": {
        "id",
        "kind",
        "key",
        "message",
        "count",
        "first_seen_at",
        "last_seen_at",
        "resolved_at",
        "resolved_by",
    },
}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def test_all_plan_tables_present() -> None:
    assert set(Base.metadata.tables) == {"sources", "news_items", *EXPECTED_COLUMNS}


def test_columns() -> None:
    for name, columns in EXPECTED_COLUMNS.items():
        assert set(_table(name).columns.keys()) == columns, name


def test_datetimes_timezone_aware() -> None:
    for name in EXPECTED_COLUMNS:
        for column in _table(name).columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone, f"{name}.{column.name}"


def test_one_processed_post_per_news_item() -> None:
    column = _table("processed_posts").c.news_item_id
    assert column.unique is True
    assert column.nullable is False


def test_keyword_unique_word_type() -> None:
    uniques = [
        [c.name for c in constraint.columns]
        for constraint in _table("keywords").constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    assert ["word", "type"] in uniques


def test_cost_is_numeric_not_float() -> None:
    cost_type = _table("processed_posts").c.cost.type
    assert isinstance(cost_type, Numeric)
    assert cost_type.asdecimal is True
    assert cost_type.scale is not None
    assert cost_type.scale > 0


def test_postgres_specific_types() -> None:
    assert isinstance(_table("processed_posts").c.hashtags.type, ARRAY)
    assert isinstance(_table("settings").c.value.type, JSONB)
    assert isinstance(_table("publications").c.channel_id.type, BigInteger)
    assert isinstance(_table("admins").c.telegram_id.type, BigInteger)


def test_primary_keys() -> None:
    assert [c.name for c in _table("admins").primary_key] == ["telegram_id"]
    assert _table("admins").c.telegram_id.autoincrement is False
    assert [c.name for c in _table("settings").primary_key] == ["key"]


def test_foreign_keys_cascade() -> None:
    chain = [
        ("processed_posts", "news_item_id", "news_items"),
        ("publications", "processed_post_id", "processed_posts"),
    ]
    for table, column, target in chain:
        [fk] = _table(table).c[column].foreign_keys
        assert fk.column.table.name == target
        assert fk.ondelete == "CASCADE"


def test_relationships_news_to_publications() -> None:
    news_rel = NewsItem.__mapper__.relationships["processed_post"]
    assert news_rel.mapper.class_ is ProcessedPost
    assert news_rel.uselist is False
    post_rels = ProcessedPost.__mapper__.relationships
    assert post_rels["news_item"].mapper.class_ is NewsItem
    assert post_rels["publications"].mapper.class_ is Publication
    assert post_rels["publications"].uselist is True
    assert Publication.__mapper__.relationships["processed_post"].mapper.class_ is ProcessedPost


def test_enum_values() -> None:
    assert [k.value for k in KeywordType] == ["include", "exclude"]
    assert [r.value for r in AdminRole] == ["admin", "owner"]
    assert [s.value for s in PublicationStatus] == ["sent", "failed"]
