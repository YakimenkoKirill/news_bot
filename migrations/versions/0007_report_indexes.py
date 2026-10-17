"""Индексы под выборки отчётов, бюджета и очереди.

Индексы создаются и удаляются CONCURRENTLY в autocommit_block: на рабочей БД это
не блокирует запись в таблицы.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17 13:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_publications_channel_published", "publications", ["channel_id", "published_at"]),
    ("ix_processed_posts_created_at", "processed_posts", ["created_at"]),
    ("ix_news_items_created_at", "news_items", ["created_at"]),
    ("ix_news_items_source_status", "news_items", ["source_id", "status"]),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, columns in INDEXES:
            op.create_index(
                name,
                table,
                columns,
                unique=False,
                postgresql_concurrently=True,
                if_not_exists=True,
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, _columns in reversed(INDEXES):
            op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
