"""Репозиторий новостей."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import BigInteger, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NewsItem

TG_MESSAGE_ID_KEY = "msg_id"
_DIGITS_RE = r"^[0-9]+$"


class RawNews(Protocol):
    """Сырая новость от сборщика (RawItem подходит структурно)."""

    @property
    def source_id(self) -> int:
        """Id источника."""
        raise NotImplementedError

    @property
    def external_id(self) -> str:
        """Идентификатор новости внутри источника."""
        raise NotImplementedError

    @property
    def url(self) -> str:
        """Ссылка на оригинал."""
        raise NotImplementedError

    @property
    def title(self) -> str:
        """Заголовок."""
        raise NotImplementedError

    @property
    def text(self) -> str:
        """Исходный текст."""
        raise NotImplementedError

    @property
    def image_url(self) -> str | None:
        """Ссылка на картинку."""
        raise NotImplementedError

    @property
    def published_at(self) -> datetime | None:
        """Время публикации в источнике."""
        raise NotImplementedError

    @property
    def raw_meta(self) -> Mapping[str, Any]:
        """Служебные данные сборщика."""
        raise NotImplementedError


class NewsRepository:
    """Доступ к таблице news_items.

    Методы не коммитят: транзакцией управляет вызывающий код (session_scope).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_new(self, raw: RawNews) -> NewsItem | None:
        """Вставляет новость, если пары (source_id, external_id) ещё нет.

        Использует INSERT ... ON CONFLICT DO NOTHING RETURNING, поэтому безопасна
        при параллельной вставке одной и той же новости.

        Args:
            raw: Сырая новость от сборщика.

        Returns:
            Созданная запись или None, если такая новость уже есть.
        """
        stmt = (
            insert(NewsItem)
            .values(
                source_id=raw.source_id,
                external_id=raw.external_id,
                url=raw.url,
                title=raw.title,
                raw_text=raw.text,
                image_url=raw.image_url,
                published_at=raw.published_at,
                raw_meta=dict(raw.raw_meta),
            )
            .on_conflict_do_nothing(index_elements=[NewsItem.source_id, NewsItem.external_id])
            .returning(NewsItem)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def exists_url(self, canonical_url: str) -> bool:
        """Проверяет, есть ли новость с таким каноническим URL."""
        stmt = select(select(NewsItem.id).where(NewsItem.canonical_url == canonical_url).exists())
        return bool(await self._session.scalar(stmt))

    async def find_by_hash(self, content_hash: str, since: datetime) -> NewsItem | None:
        """Возвращает самую раннюю новость с таким хешем, собранную не раньше since."""
        stmt = (
            select(NewsItem)
            .where(NewsItem.content_hash == content_hash, NewsItem.created_at >= since)
            .order_by(NewsItem.id)
            .limit(1)
        )
        return (await self._session.scalars(stmt)).first()

    async def recent_simhashes(self, since: datetime) -> list[tuple[int, int, int | None]]:
        """Возвращает (id, simhash, cluster_id) новостей с simhash, собранных не раньше since."""
        stmt = (
            select(NewsItem.id, NewsItem.simhash, NewsItem.cluster_id)
            .where(NewsItem.simhash.is_not(None), NewsItem.created_at >= since)
            .order_by(NewsItem.id)
        )
        rows: Sequence[Any] = (await self._session.execute(stmt)).all()
        return [(row.id, row.simhash, row.cluster_id) for row in rows]

    async def max_tg_message_id(self, source_id: int) -> int | None:
        """Возвращает наибольший raw_meta->>'msg_id' источника, сравнивая как числа."""
        msg_id = NewsItem.raw_meta[TG_MESSAGE_ID_KEY].astext
        stmt = select(func.max(msg_id.cast(BigInteger))).where(
            NewsItem.source_id == source_id, msg_id.regexp_match(_DIGITS_RE)
        )
        return await self._session.scalar(stmt)
