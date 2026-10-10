"""Начальная схема: все таблицы и индексы.

Revision ID: 0001
Revises:
Create Date: 2026-10-10 13:59:53.293460
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admins",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "role",
            sa.Enum("admin", "owner", name="adminrole", native_enum=False, length=32),
            server_default="admin",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("telegram_id", name=op.f("pk_admins")),
    )
    op.create_table(
        "keywords",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("word", sa.String(length=255), nullable=False),
        sa.Column(
            "type",
            sa.Enum("include", "exclude", name="keywordtype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_keywords")),
        sa.UniqueConstraint("word", "type", name=op.f("uq_keywords_word_type")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "type",
            sa.Enum("rss", "web", "tg", name="sourcetype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("interval_min", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "moderation_mode",
            sa.Enum("auto", "review", name="moderationmode", native_enum=False, length=32),
            server_default="auto",
            nullable=False,
        ),
        sa.Column("selectors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "fetch_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_item_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("url", name=op.f("uq_sources_url")),
    )
    op.create_table(
        "news_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("clean_text", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("simhash", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
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
                name="newsstatus",
                native_enum=False,
                length=32,
            ),
            server_default="new",
            nullable=False,
        ),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("cluster_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "raw_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["news_items.id"],
            name=op.f("fk_news_items_cluster_id_news_items"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_news_items_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_items")),
    )
    op.create_index("ix_news_items_canonical_url", "news_items", ["canonical_url"], unique=False)
    op.create_index("ix_news_items_cluster_id", "news_items", ["cluster_id"], unique=False)
    op.create_index("ix_news_items_content_hash", "news_items", ["content_hash"], unique=False)
    op.create_index(
        "ix_news_items_status_created_at", "news_items", ["status", "created_at"], unique=False
    )
    op.create_index(
        "uq_news_items_source_id_external_id",
        "news_items",
        ["source_id", "external_id"],
        unique=True,
    )
    op.create_table(
        "processed_posts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("news_item_id", sa.BigInteger(), nullable=False),
        sa.Column("ai_title", sa.Text(), nullable=False),
        sa.Column("ai_summary", sa.Text(), nullable=False),
        sa.Column(
            "hashtags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edited_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["news_item_id"],
            ["news_items.id"],
            name=op.f("fk_processed_posts_news_item_id_news_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processed_posts")),
        sa.UniqueConstraint("news_item_id", name=op.f("uq_processed_posts_news_item_id")),
    )
    op.create_table(
        "publications",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("processed_post_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum("sent", "failed", name="publicationstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["processed_post_id"],
            ["processed_posts.id"],
            name=op.f("fk_publications_processed_post_id_processed_posts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publications")),
    )
    op.create_index(
        op.f("ix_publications_processed_post_id"),
        "publications",
        ["processed_post_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_publications_processed_post_id"), table_name="publications")
    op.drop_table("publications")
    op.drop_table("processed_posts")
    op.drop_index("uq_news_items_source_id_external_id", table_name="news_items")
    op.drop_index("ix_news_items_status_created_at", table_name="news_items")
    op.drop_index("ix_news_items_content_hash", table_name="news_items")
    op.drop_index("ix_news_items_cluster_id", table_name="news_items")
    op.drop_index("ix_news_items_canonical_url", table_name="news_items")
    op.drop_table("news_items")
    op.drop_table("sources")
    op.drop_table("settings")
    op.drop_table("keywords")
    op.drop_table("admins")
