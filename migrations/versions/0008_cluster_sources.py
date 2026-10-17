"""Число источников события: news_items.cluster_sources.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = """
UPDATE news_items AS root
SET cluster_sources = (
    SELECT count(DISTINCT member.source_id)
    FROM news_items AS member
    WHERE member.id = root.id OR member.cluster_id = root.id
)
WHERE root.cluster_id IS NULL OR root.cluster_id = root.id
"""


def upgrade() -> None:
    op.add_column(
        "news_items",
        sa.Column(
            "cluster_sources",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.execute(sa.text(BACKFILL))


def downgrade() -> None:
    op.drop_column("news_items", "cluster_sources")
