"""Репозиторий новостей."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import BigInteger, ColumnElement, Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NewsItem, ProcessedPost, Publication
from app.domain.enums import NewsStatus, PublicationStatus
from app.domain.status import TERMINAL_STATUSES, ensure_transition

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
        "simhash_b0",
        "simhash_b1",
        "simhash_b2",
        "simhash_b3",
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


@dataclass(frozen=True)
class QueuedRef:
    """Пост очереди для проверки лимита источника.

    Attributes:
        news_id: Новость.
        status: Текущий статус (approved или scheduled).
        priority: Приоритет публикации; меньше — слабее.
    """

    news_id: int
    status: NewsStatus
    priority: float


@dataclass(frozen=True)
class StuckRef:
    """Новость, застрявшая в отправке в канал.

    Attributes:
        news_id: Новость в статусе scheduled.
        title: Заголовок новости (для сообщения администратору).
        sent: Есть ли запись Publication(sent) — пост всё-таки ушёл в канал.
    """

    news_id: int
    title: str
    sent: bool


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

    async def simhash_candidates(
        self, bands: Sequence[int], since: datetime
    ) -> list[tuple[int, int, int | None]]:
        """Новости окна, у которых хотя бы одна часть SimHash совпала с bands.

        Каждая часть ищется по своему индексу (simhash_bN, created_at), поэтому
        перебирать всё окно не нужно. Новости без simhash не участвуют: их части
        равны NULL и ни с чем не совпадают.

        Args:
            bands: Части SimHash проверяемой новости (simhash_bands).
            since: Начало окна поиска.

        Returns:
            Пары (id, simhash, cluster_id) в порядке id.

        Raises:
            ValueError: Число частей не совпадает с числом столбцов таблицы.
        """
        columns = (
            NewsItem.simhash_b0,
            NewsItem.simhash_b1,
            NewsItem.simhash_b2,
            NewsItem.simhash_b3,
        )
        if len(bands) != len(columns):
            raise ValueError(f"ожидалось {len(columns)} частей simhash, получено {len(bands)}")
        stmt = (
            select(NewsItem.id, NewsItem.simhash, NewsItem.cluster_id)
            .where(
                NewsItem.created_at >= since,
                or_(*(column == band for column, band in zip(columns, bands, strict=True))),
            )
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

    async def list_stuck_scheduled(self, before: datetime) -> list[StuckRef]:
        """Новости в статусе scheduled, не менявшиеся с before, сначала самые старые.

        Статус scheduled означает «отправляется»: он держится только на время
        отправки в канал, поэтому старая строка — след упавшего приложения.

        Args:
            before: Граница: берутся новости с updated_at <= before.

        Returns:
            Ссылки на застрявшие новости с признаком «есть Publication(sent)».
        """
        sent = (
            select(Publication.id)
            .join(ProcessedPost, ProcessedPost.id == Publication.processed_post_id)
            .where(
                ProcessedPost.news_item_id == NewsItem.id,
                Publication.status == PublicationStatus.SENT,
            )
            .exists()
        )
        stmt = (
            select(NewsItem.id, NewsItem.title, sent)
            .where(NewsItem.status == NewsStatus.SCHEDULED, NewsItem.updated_at <= before)
            .order_by(NewsItem.updated_at, NewsItem.id)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            StuckRef(news_id=news_id, title=title, sent=bool(has_sent))
            for news_id, title, has_sent in rows
        ]

    async def strip_texts(self, before: datetime, limit: int) -> int:
        """Очищает тексты старых новостей в конечных статусах.

        Берутся новости, которым уже некуда двигаться по машине состояний
        (TERMINAL_STATUSES), созданные раньше before и ещё не очищенные:
        raw_text становится пустым, clean_text — NULL. Заголовок, адрес, хеши,
        simhash с частями, cluster_id и raw_meta остаются: они нужны поиску
        дублей, отчётам и картинкам. Строки, занятые другой транзакцией,
        пропускаются (SKIP LOCKED), поэтому очистка не мешает нормализации.

        Args:
            before: Очищаются новости, созданные раньше этого момента.
            limit: Размер пачки.

        Returns:
            Сколько новостей очищено.

        Raises:
            ValueError: limit <= 0.
        """
        if limit <= 0:
            raise ValueError(f"limit должен быть положительным, получено {limit}")
        selected = (
            select(NewsItem.id)
            .where(
                NewsItem.status.in_(sorted(TERMINAL_STATUSES)),
                NewsItem.created_at < before,
                or_(NewsItem.raw_text != "", NewsItem.clean_text.is_not(None)),
            )
            .order_by(NewsItem.created_at, NewsItem.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(NewsItem)
            .where(NewsItem.id.in_(selected.scalar_subquery()))
            .values(raw_text="", clean_text=None)
            .returning(NewsItem.id)
            .execution_options(synchronize_session=False)
        )
        return len((await self._session.scalars(stmt)).all())

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

    async def list_failed(self, limit: int, offset: int) -> list[tuple[NewsItem, bool]]:
        """Новости в статусе failed, сначала новые, с признаком «есть пересказ».

        Returns:
            Пары (новость, есть ли ProcessedPost).

        Raises:
            ValueError: limit <= 0 или offset < 0.
        """
        if limit <= 0:
            raise ValueError(f"limit должен быть положительным, получено {limit}")
        if offset < 0:
            raise ValueError(f"offset не может быть отрицательным, получено {offset}")
        stmt = (
            select(NewsItem, ProcessedPost.id.is_not(None))
            .outerjoin(ProcessedPost, ProcessedPost.news_item_id == NewsItem.id)
            .where(NewsItem.status == NewsStatus.FAILED)
            .order_by(NewsItem.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(news, bool(has_post)) for news, has_post in rows]

    async def count_failed(self) -> int:
        """Число новостей в статусе failed."""
        stmt = (
            select(func.count()).select_from(NewsItem).where(NewsItem.status == NewsStatus.FAILED)
        )
        return int(await self._session.scalar(stmt) or 0)

    async def queued_by_source(
        self, source_id: int, statuses: Sequence[NewsStatus], decay_per_h: float
    ) -> list[QueuedRef]:
        """Посты источника в очереди, от самого слабого к самому сильному.

        Порядок обратен list_prepared: первым идёт тот, кого вытеснять.

        Raises:
            ValueError: Пустой список статусов или decay_per_h < 0.
        """
        checked = _check_statuses(statuses)
        _check_decay(decay_per_h)
        priority = _priority(decay_per_h)
        stmt = (
            select(NewsItem.id, NewsItem.status, priority)
            .join(ProcessedPost, ProcessedPost.news_item_id == NewsItem.id)
            .where(NewsItem.source_id == source_id, NewsItem.status.in_(checked))
            .order_by(priority, NewsItem.id.desc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            QueuedRef(news_id=row[0], status=NewsStatus(row[1]), priority=row[2]) for row in rows
        ]

    async def priority_of(self, news_id: int, decay_per_h: float) -> float | None:
        """Приоритет новости или None, если новости нет или у неё нет пересказа.

        Raises:
            ValueError: decay_per_h < 0.
        """
        _check_decay(decay_per_h)
        stmt = (
            select(_priority(decay_per_h))
            .select_from(NewsItem)
            .join(ProcessedPost, ProcessedPost.news_item_id == NewsItem.id)
            .where(NewsItem.id == news_id)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def queue_counts_by_source(self, statuses: Sequence[NewsStatus]) -> dict[int, int]:
        """Сколько постов в очереди у каждого источника (только с пересказом).

        Raises:
            ValueError: Пустой список статусов.
        """
        checked = _check_statuses(statuses)
        stmt = (
            select(NewsItem.source_id, func.count())
            .join(ProcessedPost, ProcessedPost.news_item_id == NewsItem.id)
            .where(NewsItem.status.in_(checked))
            .group_by(NewsItem.source_id)
        )
        return dict((await self._session.execute(stmt)).all())

    async def count_by_status(self) -> dict[NewsStatus, int]:
        """Возвращает число новостей в каждом статусе, включая нулевые."""
        stmt = select(NewsItem.status, func.count()).group_by(NewsItem.status)
        counts = {status: 0 for status in NewsStatus}
        for status, count in (await self._session.execute(stmt)).all():
            counts[NewsStatus(status)] = count
        return counts
