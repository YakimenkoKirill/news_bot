"""Модели SQLAlchemy."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.domain.enums import (
    AdminRole,
    KeywordType,
    ModerationMode,
    NewsStatus,
    PublicationStatus,
    SourceType,
)

COST_PRECISION = 12
COST_SCALE = 6

ENUM_LENGTH = 32


def _enum_column(enum_cls: type[PyEnum]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        length=ENUM_LENGTH,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


def _tz() -> DateTime:
    return DateTime(timezone=True)


class Source(Base):
    """Источник новостей: RSS-лента, сайт или Telegram-канал."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[SourceType] = mapped_column(_enum_column(SourceType))
    url: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(String(255))
    interval_min: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    moderation_mode: Mapped[ModerationMode] = mapped_column(
        _enum_column(ModerationMode),
        default=ModerationMode.AUTO,
        server_default=ModerationMode.AUTO.value,
    )
    selectors: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    fetch_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(_tz(), nullable=True)
    last_item_at: Mapped[datetime | None] = mapped_column(_tz(), nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(_tz(), server_default=func.now())


class NewsItem(Base):
    """Новость, собранная из источника, и её путь по пайплайну."""

    __tablename__ = "news_items"
    __table_args__ = (
        Index("uq_news_items_source_id_external_id", "source_id", "external_id", unique=True),
        Index("ix_news_items_canonical_url", "canonical_url"),
        Index("ix_news_items_content_hash", "content_hash"),
        Index("ix_news_items_status_created_at", "status", "created_at"),
        Index("ix_news_items_cluster_id", "cluster_id"),
        Index("ix_news_items_created_at", "created_at"),
        Index("ix_news_items_source_status", "source_id", "status"),
        Index("ix_news_items_simhash_b0_created_at", "simhash_b0", "created_at"),
        Index("ix_news_items_simhash_b1_created_at", "simhash_b1", "created_at"),
        Index("ix_news_items_simhash_b2_created_at", "simhash_b2", "created_at"),
        Index("ix_news_items_simhash_b3_created_at", "simhash_b3", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text)
    clean_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(_tz(), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    simhash_b0: Mapped[int | None] = mapped_column(Integer, nullable=True)
    simhash_b1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    simhash_b2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    simhash_b3: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[NewsStatus] = mapped_column(
        _enum_column(NewsStatus),
        default=NewsStatus.NEW,
        server_default=NewsStatus.NEW.value,
    )
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_items.id", ondelete="SET NULL"), nullable=True
    )
    cluster_sources: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    raw_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(_tz(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        _tz(), server_default=func.now(), onupdate=func.now()
    )

    processed_post: Mapped[ProcessedPost | None] = relationship(
        back_populates="news_item", uselist=False, passive_deletes=True, lazy="raise"
    )


class ProcessedPost(Base):
    """Пересказ новости, подготовленный моделью; не больше одного на новость."""

    __tablename__ = "processed_posts"
    __table_args__ = (Index("ix_processed_posts_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_item_id: Mapped[int] = mapped_column(
        ForeignKey("news_items.id", ondelete="CASCADE"), unique=True
    )
    ai_title: Mapped[str] = mapped_column(Text)
    ai_summary: Mapped[str] = mapped_column(Text)
    hashtags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'::text[]")
    )
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relevance_score: Mapped[float] = mapped_column(Float)
    prompt_version: Mapped[str] = mapped_column(String(32))
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    completion_tokens: Mapped[int] = mapped_column(Integer)
    cost: Mapped[Decimal] = mapped_column(Numeric(COST_PRECISION, COST_SCALE))
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(_tz(), nullable=True)
    edited_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_tz(), server_default=func.now())

    news_item: Mapped[NewsItem] = relationship(back_populates="processed_post", lazy="raise")
    publications: Mapped[list[Publication]] = relationship(
        back_populates="processed_post", passive_deletes=True, lazy="raise"
    )


class Publication(Base):
    """Попытка публикации поста в канал."""

    __tablename__ = "publications"
    __table_args__ = (Index("ix_publications_channel_published", "channel_id", "published_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_post_id: Mapped[int] = mapped_column(
        ForeignKey("processed_posts.id", ondelete="CASCADE"), index=True
    )
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(_tz(), nullable=True)
    status: Mapped[PublicationStatus] = mapped_column(_enum_column(PublicationStatus))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    initiated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forwards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reactions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stats_at: Mapped[datetime | None] = mapped_column(_tz(), nullable=True)

    processed_post: Mapped[ProcessedPost] = relationship(
        back_populates="publications", lazy="raise"
    )


class Keyword(Base):
    """Ключевое слово фильтра: include — обязательное, exclude — стоп-слово."""

    __tablename__ = "keywords"
    __table_args__ = (UniqueConstraint("word", "type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String(255))
    type: Mapped[KeywordType] = mapped_column(_enum_column(KeywordType))


class Admin(Base):
    """Администратор бота управления."""

    __tablename__ = "admins"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    role: Mapped[AdminRole] = mapped_column(
        _enum_column(AdminRole), default=AdminRole.ADMIN, server_default=AdminRole.ADMIN.value
    )
    created_at: Mapped[datetime] = mapped_column(_tz(), server_default=func.now())


class Incident(Base):
    """Инцидент журнала ошибок: события одного вида с одним key, пока он открыт.

    Повтор события с тем же key увеличивает count у открытого инцидента; после
    закрытия (resolved_at) следующее событие открывает новый инцидент. Поэтому
    уникальность key — частичная, только среди открытых.
    """

    __tablename__ = "incidents"
    __table_args__ = (
        Index(
            "uq_incidents_open_key",
            "key",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
        Index("ix_incidents_resolved_last_seen", "resolved_at", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(ENUM_LENGTH))
    key: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    count: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    first_seen_at: Mapped[datetime] = mapped_column(_tz())
    last_seen_at: Mapped[datetime] = mapped_column(_tz())
    resolved_at: Mapped[datetime | None] = mapped_column(_tz(), nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AiRequest(Base):
    """Один запрос к модели: токены, кэш и стоимость.

    Пишется на каждую попытку, в том числе неудачную, поэтому сумма cost за сутки
    равна реально оплаченному — в отличие от processed_posts.cost, где нет новостей,
    по которым модель так и не дала разбираемый ответ.
    """

    __tablename__ = "ai_requests"
    __table_args__ = (Index("ix_ai_requests_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_items.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer)
    cost: Mapped[Decimal] = mapped_column(Numeric(COST_PRECISION, COST_SCALE))
    ok: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(_tz(), server_default=func.now())


class BotScreen(Base):
    """Текущий экран чата с ботом: сообщения, которые висят у администратора.

    Экран — группа сообщений, отправленных в ответ на одно событие. Новый экран
    приходит на смену прошлому, поэтому список хранится в БД: после перезапуска
    приложения бот всё равно знает, что удалять.
    """

    __tablename__ = "bot_screens"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    screen_token: Mapped[str] = mapped_column(String(64))
    message_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), default=list, server_default=text("'{}'::bigint[]")
    )
    updated_at: Mapped[datetime] = mapped_column(_tz(), server_default=func.now())


class Setting(Base):
    """Изменяемая во время работы настройка (например, пауза публикаций)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        _tz(), server_default=func.now(), onupdate=func.now()
    )
