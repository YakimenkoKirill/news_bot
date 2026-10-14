"""Репозиторий новостей."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import BigInteger, ColumnElement, Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NewsItem, ProcessedPost
from app.domain.enums import NewsStatus
from app.domain.status import ensure_transition

TG_MESSAGE_ID_KEY = "msg_id"
_DIGITS_RE = r"^[0-9]+$"
_SECONDS_PER_HOUR = 3600

UPDATABLE_FIELDS = frozenset(
    {
        "canonical_url",
        "title",
        "clean_text",
        "image_url",
        "language",
        "published_at",
        "content_hash",
        "simhash",
        "cluster_id",
        "raw_meta",
    }
)


class StaleStatusError(RuntimeError):
    """Статус новости уже не совпадает с ожидаемым (её изменил кто-то другой)."""

    def __init__(self, news_id: int, expected: NewsStatus) -> None:
        self.news_id = news_id
        self.expected = expected
        super().__init__(f"новость {news_id} уже не в статусе {expected.value}")


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


def _priority(decay_per_h: float) -> ColumnElement[float]:
    """Ключ сортировки очереди публикации (больше — раньше).

    priority = score − decay × возраст_в_часах. Слагаемое −decay × now одинаково
    для всех постов, поэтому сортировка идёт по score + decay × epoch(время) / 3600:
    порядок не зависит от текущего времени.
    """
    published = func.coalesce(NewsItem.published_at, NewsItem.created_at)
    hours = func.extract("epoch", published) / _SECONDS_PER_HOUR
    return ProcessedPost.relevance_score + decay_per_h * hours


def _check_statuses(statuses: Sequence[NewsStatus]) -> list[NewsStatus]:
    result = list(statuses)
    if not result:
        raise ValueError("не задан ни один статус")
    return result


def _check_decay(decay_per_h: float) -> None:
    if decay_per_h < 0:
        raise ValueError(f"decay_per_h не может быть отрицательным, получено {decay_per_h}")


def _prepared_query(
    statuses: Sequence[NewsStatus], decay_per_h: float
) -> Select[NewsItem, ProcessedPost]:
    return (
        select(NewsItem, ProcessedPost)
        .join(ProcessedPost, ProcessedPost.news_item_id == NewsItem.id)
        .where(NewsItem.status.in_(statuses))
        .order_by(_priority(decay_per_h).desc(), NewsItem.id)
    )


class NewsRepository:
    """Доступ к таблице news_items.

    Методы не коммитят: транзакцией управляет вызывающий код (session_scope).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, news_id: int) -> NewsItem | None:
        """Возвращает новость по id или None."""
        return await self._session.get(NewsItem, news_id)

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

    async def find_by_url(
        self, canonical_url: str, since: datetime, exclude_id: int | None = None
    ) -> NewsItem | None:
        """Самая ранняя новость с таким каноническим URL, собранная не раньше since."""
        stmt = (
            select(NewsItem)
            .where(NewsItem.canonical_url == canonical_url, NewsItem.created_at >= since)
            .order_by(NewsItem.id)
            .limit(1)
        )
        if exclude_id is not None:
            stmt = stmt.where(NewsItem.id != exclude_id)
        return (await self._session.scalars(stmt)).first()

    async def find_by_hash(
        self, content_hash: str, since: datetime, exclude_id: int | None = None
    ) -> NewsItem | None:
        """Возвращает самую раннюю новость с таким хешем, собранную не раньше since."""
        stmt = (
            select(NewsItem)
            .where(NewsItem.content_hash == content_hash, NewsItem.created_at >= since)
            .order_by(NewsItem.id)
            .limit(1)
        )
        if exclude_id is not None:
            stmt = stmt.where(NewsItem.id != exclude_id)
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

    async def set_status(
        self,
        news_id: int,
        expected: NewsStatus,
        new: NewsStatus,
        reason: str | None = None,
    ) -> None:
        """Переводит новость из expected в new.

        Переход проверяется машиной состояний, а UPDATE выполняется только при
        status = expected (оптимистическая блокировка).

        Args:
            news_id: Id новости.
            expected: Статус, в котором новость должна находиться сейчас.
            new: Новый статус.
            reason: Причина отказа (reject_reason); None не меняет сохранённую причину.

        Raises:
            InvalidTransition: Переход expected -> new запрещён.
            StaleStatusError: Новости нет или её статус уже не expected.
        """
        ensure_transition(expected, new)
        values: dict[str, Any] = {"status": new}
        if reason is not None:
            values["reject_reason"] = reason
        stmt = (
            update(NewsItem)
            .where(NewsItem.id == news_id, NewsItem.status == expected)
            .values(**values)
            .returning(NewsItem)
            .execution_options(populate_existing=True)
        )
        if (await self._session.scalars(stmt)).one_or_none() is None:
            raise StaleStatusError(news_id, expected)

    async def claim_batch(
        self, status: NewsStatus | Sequence[NewsStatus], limit: int
    ) -> list[NewsItem]:
        """Захватывает до limit самых старых новостей в статусе (или одном из статусов).

        Использует SELECT ... FOR UPDATE SKIP LOCKED: строки остаются заблокированными
        до конца транзакции вызывающего, параллельные обработчики получают другие строки.

        Raises:
            ValueError: limit <= 0 или пустой список статусов.
        """
        if limit <= 0:
            raise ValueError(f"limit должен быть положительным, получено {limit}")
        statuses = [status] if isinstance(status, NewsStatus) else list(status)
        if not statuses:
            raise ValueError("не задан ни один статус")
        stmt = (
            select(NewsItem)
            .where(NewsItem.status.in_(statuses))
            .order_by(NewsItem.created_at, NewsItem.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(stmt)).all())

    async def list_prepared(
        self,
        statuses: Sequence[NewsStatus],
        decay_per_h: float,
        limit: int,
        offset: int,
    ) -> list[tuple[NewsItem, ProcessedPost]]:
        """Новости с пересказом в одном из статусов, по приоритету публикации.

        Без блокировок строк; порядок совпадает с последовательными claim_next.

        Args:
            statuses: Статусы новостей.
            decay_per_h: Снижение оценки за час возраста (0 — только по оценке).
            limit: Сколько строк вернуть.
            offset: Сколько строк пропустить.

        Returns:
            Пары (новость, пересказ).

        Raises:
            ValueError: Пустой список статусов, limit <= 0, offset < 0 или decay_per_h < 0.
        """
        checked = _check_statuses(statuses)
        _check_decay(decay_per_h)
        if limit <= 0:
            raise ValueError(f"limit должен быть положительным, получено {limit}")
        if offset < 0:
            raise ValueError(f"offset не может быть отрицательным, получено {offset}")
        stmt = _prepared_query(checked, decay_per_h).limit(limit).offset(offset)
        rows = (await self._session.execute(stmt)).all()
        return [(news, post) for news, post in rows]

    async def count_prepared(self, statuses: Sequence[NewsStatus]) -> int:
        """Число новостей с пересказом в одном из статусов.

        Raises:
            ValueError: Пустой список статусов.
        """
        checked = _check_statuses(statuses)
        stmt = (
            select(func.count())
            .select_from(NewsItem)
            .join(ProcessedPost, ProcessedPost.news_item_id == NewsItem.id)
            .where(NewsItem.status.in_(checked))
        )
        return int(await self._session.scalar(stmt) or 0)

    async def claim_next(
        self, statuses: Sequence[NewsStatus], decay_per_h: float
    ) -> NewsItem | None:
        """Захватывает пост с наибольшим приоритетом (FOR UPDATE SKIP LOCKED).

        Строки, занятые другими транзакциями, пропускаются; блокировка держится до
        конца транзакции вызывающего.

        Raises:
            ValueError: Пустой список статусов или decay_per_h < 0.
        """
        checked = _check_statuses(statuses)
        _check_decay(decay_per_h)
        stmt = (
            _prepared_query(checked, decay_per_h)
            .limit(1)
            .with_for_update(skip_locked=True, of=NewsItem)
            .execution_options(populate_existing=True)
        )
        row = (await self._session.execute(stmt)).first()
        return row[0] if row is not None else None

    async def claim_one(self, news_id: int, statuses: Sequence[NewsStatus]) -> NewsItem | None:
        """Захватывает новость по id, если она в одном из статусов и не занята.

        Не ждёт чужую блокировку: занятая строка, другой статус или несуществующий
        id дают None. Блокировка держится до конца транзакции вызывающего.

        Raises:
            ValueError: Пустой список статусов.
        """
        checked = _check_statuses(statuses)
        stmt = (
            select(NewsItem)
            .where(NewsItem.id == news_id, NewsItem.status.in_(checked))
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def update_fields(self, news_id: int, **fields: Any) -> bool:
        """Обновляет поля, заполняемые нормализацией и дедупликацией.

        Статус так не меняется — только через set_status.

        Raises:
            ValueError: Поле не входит в UPDATABLE_FIELDS или полей нет.
        """
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown or not fields:
            raise ValueError(f"нельзя обновить поля: {sorted(unknown) or 'пустой набор'}")
        stmt = (
            update(NewsItem)
            .where(NewsItem.id == news_id)
            .values(**fields)
            .returning(NewsItem)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none() is not None

    async def count_by_status(self) -> dict[NewsStatus, int]:
        """Возвращает число новостей в каждом статусе, включая нулевые."""
        stmt = select(NewsItem.status, func.count()).group_by(NewsItem.status)
        counts = {status: 0 for status in NewsStatus}
        for status, count in (await self._session.execute(stmt)).all():
            counts[NewsStatus(status)] = count
        return counts
