"""Журнал ошибок: таблица incidents.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14 14:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incidents")),
    )
    op.create_index(
        "uq_incidents_open_key",
        "incidents",
        ["key"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index(
        "ix_incidents_resolved_last_seen",
        "incidents",
        ["resolved_at", "last_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_incidents_resolved_last_seen", table_name="incidents")
    op.drop_index(
        "uq_incidents_open_key",
        table_name="incidents",
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.drop_table("incidents")
