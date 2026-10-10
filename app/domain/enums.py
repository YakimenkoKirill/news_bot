"""Перечисления предметной области."""

from enum import StrEnum


class SourceType(StrEnum):
    """Тип источника новостей."""

    RSS = "rss"
    WEB = "web"
    TG = "tg"


class ModerationMode(StrEnum):
    """Режим модерации постов источника."""

    AUTO = "auto"
    REVIEW = "review"


class NewsStatus(StrEnum):
    """Статус новости в жизненном цикле пайплайна."""

    NEW = "new"
    DUPLICATE = "duplicate"
    FILTERED = "filtered"
    NORMALIZED = "normalized"
    PROCESSED = "processed"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"


class KeywordType(StrEnum):
    """Тип ключевого слова: обязательное или стоп-слово."""

    INCLUDE = "include"
    EXCLUDE = "exclude"


class AdminRole(StrEnum):
    """Роль администратора бота."""

    ADMIN = "admin"
    OWNER = "owner"


class PublicationStatus(StrEnum):
    """Результат отправки поста в канал."""

    SENT = "sent"
    FAILED = "failed"
