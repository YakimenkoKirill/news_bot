"""Статистика постов канала: просмотры, пересылки и реакции.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-17 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("views", sa.Integer()),
    ("forwards", sa.Integer()),
    ("reactions", sa.Integer()),
    ("stats_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, column_type in COLUMNS:
        op.add_column("publications", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    for name, _column_type in reversed(COLUMNS):
        op.drop_column("publications", name)
