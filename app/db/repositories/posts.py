"""Репозитории обработанных постов и публикаций."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NewsItem, ProcessedPost, Publication
from app.domain.enums import NewsStatus, PublicationStatus


class ProcessedPostRepository:
    """Доступ к таблице processed_posts. Методы не коммитят."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, post: ProcessedPost) -> ProcessedPost:
        """Сохраняет пересказ и возвращает его с присвоенным id."""
        self._session.add(post)
        await self._session.flush()
        return post

    async def get(self, post_id: int) -> ProcessedPost | None:
        """Возвращает пост по id или None."""
        return await self._session.get(ProcessedPost, post_id)

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        """Возвращает пересказ новости или None."""
        stmt = select(ProcessedPost).where(ProcessedPost.news_item_id == news_id)
        return (await self._session.scalars(stmt)).one_or_none()

    async def update_summary(self, post_id: int, summary: str, edited_by: int) -> bool:
        """Заменяет пересказ, отредактированный администратором."""
        return await self._update(post_id, ai_summary=summary, edited_by=edited_by)

    async def set_snooze(self, post_id: int, until: datetime | None) -> bool:
        """Откладывает повторный показ поста на модерации до until (None — снять)."""
        return await self._update(post_id, snoozed_until=until)

    async def list_due_reviews(self, now: datetime) -> Sequence[ProcessedPost]:
        """Возвращает отложенные посты на модерации, срок которых наступил.

        Пост попадает в выборку, если его новость в статусе pending_review и
        snoozed_until <= now; посты с snoozed_until в будущем или без отсрочки не входят.

        Args:
            now: Текущий момент (timezone-aware).

        Returns:
            Посты в порядке snoozed_until.
        """
        stmt = (
            select(ProcessedPost)
            .join(NewsItem, NewsItem.id == ProcessedPost.news_item_id)
            .where(
                NewsItem.status == NewsStatus.PENDING_REVIEW,
                ProcessedPost.snoozed_until <= now,
            )
            .order_by(ProcessedPost.snoozed_until, ProcessedPost.id)
        )
        return (await self._session.scalars(stmt)).all()

    async def cost_since(self, dt: datetime) -> Decimal:
        """Суммирует стоимость обработки постов, созданных начиная с dt."""
        stmt = select(func.coalesce(func.sum(ProcessedPost.cost), 0)).where(
            ProcessedPost.created_at >= dt
        )
        return Decimal((await self._session.scalar(stmt)) or 0)

    async def _update(self, post_id: int, **values: Any) -> bool:
        stmt = (
            update(ProcessedPost)
            .where(ProcessedPost.id == post_id)
            .values(**values)
            .returning(ProcessedPost)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none() is not None


@dataclass(frozen=True)
class PendingStats:
    """Публикация, которой ещё не собрали статистику.

    Attributes:
        publication_id: Запись publications.
        message_id: Сообщение в канале.
    """

    publication_id: int
    message_id: int


class PublicationRepository:
    """Доступ к таблице publications. Методы не коммитят."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, publication: Publication) -> Publication:
        """Сохраняет запись о публикации и возвращает её с присвоенным id."""
        self._session.add(publication)
        await self._session.flush()
        return publication

    async def mark_sent(self, publication_id: int, message_id: int, at: datetime) -> bool:
        """Отмечает успешную отправку в канал."""
        return await self._update(
            publication_id,
            status=PublicationStatus.SENT,
            message_id=message_id,
            published_at=at,
            error=None,
        )

    async def mark_failed(self, publication_id: int, error: str) -> bool:
        """Отмечает неудачную отправку."""
        return await self._update(publication_id, status=PublicationStatus.FAILED, error=error)

    async def last_published_at(self, channel_id: int) -> datetime | None:
        """Время последней успешной (status=sent) публикации в канал или None."""
        stmt = select(func.max(Publication.published_at)).where(
            Publication.channel_id == channel_id,
            Publication.status == PublicationStatus.SENT,
        )
        return await self._session.scalar(stmt)

    async def list_for_stats(
        self, channel_id: int, since: datetime, until: datetime, limit: int
    ) -> list[PendingStats]:
        """Отправленные посты канала, которым пора собрать статистику.

        Берутся публикации со status = sent и ещё не собранной статистикой
        (stats_at IS NULL), вышедшие в окне [since, until]: пост должен успеть
        набрать просмотры (until = now − stats.delay_h), но не быть слишком
        старым (since = now − stats.max_age_h).

        Args:
            channel_id: Канал публикации.
            since: Нижняя граница времени выхода.
            until: Верхняя граница времени выхода.
            limit: Сколько публикаций вернуть.

        Returns:
            Публикации от самых старых к новым.

        Raises:
            ValueError: limit <= 0.
        """
        if limit <= 0:
            raise ValueError(f"limit должен быть положительным, получено {limit}")
        stmt = (
            select(Publication.id, Publication.message_id)
            .where(
                Publication.channel_id == channel_id,
                Publication.status == PublicationStatus.SENT,
                Publication.stats_at.is_(None),
                Publication.message_id.is_not(None),
                Publication.published_at >= since,
                Publication.published_at <= until,
            )
            .order_by(Publication.published_at, Publication.id)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            PendingStats(publication_id=publication_id, message_id=message_id)
            for publication_id, message_id in rows
            if message_id is not None
        ]

    async def save_stats(
        self,
        publication_id: int,
        at: datetime,
        *,
        views: int | None = None,
        forwards: int | None = None,
        reactions: int | None = None,
    ) -> bool:
        """Записывает статистику поста; None остаётся, если данных нет.

        Args:
            publication_id: Запись publications.
            at: Когда статистика собрана.
            views: Просмотры.
            forwards: Пересылки.
            reactions: Сумма реакций.

        Returns:
            True, если запись найдена и обновлена.
        """
        return await self._update(
            publication_id,
            views=views,
            forwards=forwards,
            reactions=reactions,
            stats_at=at,
        )

    async def give_up_stats(self, channel_id: int, before: datetime, at: datetime) -> int:
        """Ставит stats_at постам, которые уже поздно опрашивать.

        Значения просмотров остаются NULL: статистика так и не собрана, но
        запрашивать её вечно не нужно.

        Args:
            channel_id: Канал публикации.
            before: Публикации, вышедшие раньше этого момента.
            at: Время отметки.

        Returns:
            Сколько публикаций отмечено.
        """
        stmt = (
            update(Publication)
            .where(
                Publication.channel_id == channel_id,
                Publication.status == PublicationStatus.SENT,
                Publication.stats_at.is_(None),
                Publication.published_at < before,
            )
            .values(stats_at=at)
            .returning(Publication.id)
            .execution_options(synchronize_session=False)
        )
        return len((await self._session.scalars(stmt)).all())

    async def _update(self, publication_id: int, **values: Any) -> bool:
        stmt = (
            update(Publication)
            .where(Publication.id == publication_id)
            .values(**values)
            .returning(Publication)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none() is not None
