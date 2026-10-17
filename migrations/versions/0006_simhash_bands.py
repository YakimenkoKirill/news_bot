"""Части SimHash для поиска похожих новостей по индексу.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BANDS = 4
BAND_BITS = 16
BAND_MASK = (1 << BAND_BITS) - 1


def _band_expression(index: int) -> str:
    """SQL-выражение части simhash.

    Сдвиг и маска работают и для отрицательных значений: в BIGINT лежат те же
    64 бита беззнакового SimHash, а маска оставляет только нужные 16 бит.

    Args:
        index: Номер части от старших бит (0) к младшим (3).

    Returns:
        Выражение для SELECT и UPDATE.
    """
    shift = BAND_BITS * (BANDS - 1 - index)
    return f"((simhash >> {shift}) & {BAND_MASK})"


def upgrade() -> None:
    for index in range(BANDS):
        column = f"simhash_b{index}"
        op.add_column("news_items", sa.Column(column, sa.Integer(), nullable=True))
        op.create_index(
            f"ix_news_items_{column}_created_at",
            "news_items",
            [column, "created_at"],
            unique=False,
        )
    assignments = ", ".join(
        f"simhash_b{index} = {_band_expression(index)}" for index in range(BANDS)
    )
    op.execute(sa.text(f"UPDATE news_items SET {assignments} WHERE simhash IS NOT NULL"))


def downgrade() -> None:
    for index in reversed(range(BANDS)):
        column = f"simhash_b{index}"
        op.drop_index(f"ix_news_items_{column}_created_at", table_name="news_items")
        op.drop_column("news_items", column)
