"""Учёт запросов к модели: таблица ai_requests.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COST_PRECISION = 12
COST_SCALE = 6
KIND_SUMMARY = "summary"


def upgrade() -> None:
    op.create_table(
        "ai_requests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("news_item_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(COST_PRECISION, COST_SCALE), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["news_item_id"],
            ["news_items.id"],
            name=op.f("fk_ai_requests_news_item_id_news_items"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_requests")),
    )
    op.create_index("ix_ai_requests_created_at", "ai_requests", ["created_at"], unique=False)
    op.execute(
        sa.text(
            "INSERT INTO ai_requests (news_item_id, kind, model, prompt_tokens, cached_tokens, "
            "completion_tokens, cost, ok, created_at) "
            "SELECT news_item_id, :kind, '', prompt_tokens, 0, completion_tokens, cost, true, "
            "created_at FROM processed_posts"
        ).bindparams(kind=KIND_SUMMARY)
    )


def downgrade() -> None:
    op.drop_index("ix_ai_requests_created_at", table_name="ai_requests")
    op.drop_table("ai_requests")
