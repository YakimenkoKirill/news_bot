"""Стадия нормализации: очистка, фильтры и дедупликация новых новостей."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import structlog

from app.config import DedupConfig, FiltersConfig, NormalizeConfig
from app.domain.enums import KeywordType, NewsStatus
from app.logging import bind_context
from app.pipeline.dedup import Deduplicator, DedupRepo, compute_simhash, simhash_bands, to_db
from app.pipeline.filters import KeywordSets, apply_filters
from app.pipeline.normalizer import normalize_item

log = structlog.get_logger(__name__)

REASON_NORMALIZE_ERROR = "normalize_error"
REASON_DUPLICATE = "duplicate"


class NewNews(Protocol):
    """Поля захваченной новости (NewsItem подходит структурно)."""

    id: int
    url: str
    title: str
    raw_text: str
    published_at: datetime | None
    created_at: datetime


class NormalizeNewsRepo(DedupRepo, Protocol):
    """Нужная стадии часть NewsRepository."""

    async def claim_batch(self, status: NewsStatus, limit: int) -> Sequence[NewNews]:
        """Захватывает пачку новостей."""
        raise NotImplementedError

    async def update_fields(self, news_id: int, **fields: Any) -> bool:
        """Обновляет нормализованные поля."""
        raise NotImplementedError

    async def set_status(
        self,
        news_id: int,
        expected: NewsStatus,
        new: NewsStatus,
        reason: str | None = None,
    ) -> None:
        """Меняет статус через машину состояний."""
        raise NotImplementedError

    async def refresh_cluster_sources(self, cluster_id: int) -> int:
        """Пересчитывает число источников кластера у его корня."""
        raise NotImplementedError


class KeywordWord(Protocol):
    """Ключевое слово (Keyword подходит структурно)."""

    word: str


class KeywordSource(Protocol):
    """Нужная стадии часть KeywordRepository."""

    async def list_by_type(self, type: KeywordType) -> Sequence[KeywordWord]:
        """Слова заданного типа."""
        raise NotImplementedError


@dataclass(frozen=True)
class NormalizeDeps:
    """Зависимости стадии нормализации.

    Attributes:
        session_scope: Открывает транзакцию БД.
        news_repo: Репозиторий новостей для сессии.
        keyword_repo: Репозиторий ключевых слов для сессии.
        savepoint: Вложенная транзакция для одной новости (session.begin_nested).
        normalize: Секция normalize.
        filters: Секция filters.
        dedup: Секция dedup.
        batch_size: Размер пачки (pipeline.batch_size).
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    news_repo: Callable[[Any], NormalizeNewsRepo]
    keyword_repo: Callable[[Any], KeywordSource]
    savepoint: Callable[[Any], AbstractAsyncContextManager[Any]]
    normalize: NormalizeConfig
    filters: FiltersConfig
    dedup: DedupConfig
    batch_size: int


@dataclass(frozen=True)
class StageReport:
    """Итоги стадии."""

    claimed: int = 0
    normalized: int = 0
    filtered: int = 0
    duplicates: int = 0
    errors: int = 0


@dataclass
class _FilterView:
    title: str
    clean_text: str | None
    raw_text: str
    language: str | None
    published_at: datetime | None
    created_at: datetime


async def _load_keywords(repo: KeywordSource) -> KeywordSets:
    include = [k.word for k in await repo.list_by_type(KeywordType.INCLUDE)]
    exclude = [k.word for k in await repo.list_by_type(KeywordType.EXCLUDE)]
    return KeywordSets(include=include, exclude=exclude)


async def process_new_batch(now: datetime, deps: NormalizeDeps) -> StageReport:
    """Обрабатывает пачку новостей в статусе new в одной транзакции.

    Для каждой новости: normalize_item -> update_fields -> apply_filters ->
    Deduplicator -> set_status (filtered, duplicate или normalized). Ошибка одной
    новости откатывает только её изменения (savepoint) и переводит её в filtered
    с причиной normalize_error.

    Args:
        now: Текущий момент.
        deps: Зависимости.

    Returns:
        Сколько новостей захвачено и чем закончилась их обработка.
    """
    counts = {"normalized": 0, "filtered": 0, "duplicates": 0, "errors": 0}
    async with deps.session_scope() as session:
        news = deps.news_repo(session)
        batch = list(await news.claim_batch(NewsStatus.NEW, deps.batch_size))
        if not batch:
            return StageReport()
        keywords = await _load_keywords(deps.keyword_repo(session))
        deduplicator = Deduplicator(news, deps.dedup)
        for item in batch:
            with bind_context(news_id=item.id):
                try:
                    async with deps.savepoint(session):
                        outcome = await _process_item(item, now, news, keywords, deduplicator, deps)
                except Exception:
                    log.exception("normalize.item_failed")
                    await news.set_status(
                        item.id, NewsStatus.NEW, NewsStatus.FILTERED, REASON_NORMALIZE_ERROR
                    )
                    counts["errors"] += 1
                    counts["filtered"] += 1
                    continue
                counts[outcome] += 1

    report = StageReport(claimed=len(batch), **counts)
    log.info(
        "normalize.batch_done",
        claimed=report.claimed,
        normalized=report.normalized,
        filtered=report.filtered,
        duplicates=report.duplicates,
        errors=report.errors,
    )
    return report


async def _process_item(
    item: NewNews,
    now: datetime,
    news: NormalizeNewsRepo,
    keywords: KeywordSets,
    deduplicator: Deduplicator,
    deps: NormalizeDeps,
) -> str:
    """Обрабатывает одну новость; возвращает ключ счётчика отчёта."""
    fields = normalize_item(item, deps.normalize)
    simhash = compute_simhash(item.title, fields.clean_text, deps.dedup.first_paragraph_chars)
    bands = simhash_bands(simhash)
    await news.update_fields(
        item.id,
        clean_text=fields.clean_text,
        canonical_url=fields.canonical_url,
        language=fields.language,
        content_hash=fields.content_hash,
        simhash=to_db(simhash),
        simhash_b0=bands[0],
        simhash_b1=bands[1],
        simhash_b2=bands[2],
        simhash_b3=bands[3],
    )

    view = _FilterView(
        title=item.title,
        clean_text=fields.clean_text,
        raw_text=item.raw_text,
        language=fields.language,
        published_at=item.published_at,
        created_at=item.created_at,
    )
    decision = apply_filters(view, now, keywords, deps.filters)
    if not decision.passed:
        await news.set_status(item.id, NewsStatus.NEW, NewsStatus.FILTERED, decision.reason)
        log.info("normalize.filtered", reason=decision.reason)
        return "filtered"

    result = await deduplicator.check(item.id, fields, simhash, now)
    await news.update_fields(item.id, cluster_id=result.cluster_id)
    if result.is_duplicate:
        level = result.level.value if result.level else "unknown"
        reason = f"{REASON_DUPLICATE}:{level}:{result.duplicate_of}"
        await news.set_status(item.id, NewsStatus.NEW, NewsStatus.DUPLICATE, reason)
        sources = await news.refresh_cluster_sources(result.cluster_id)
        log.info(
            "normalize.duplicate",
            reason=reason,
            cluster_id=result.cluster_id,
            cluster_sources=sources,
        )
        return "duplicates"
    await news.set_status(item.id, NewsStatus.NEW, NewsStatus.NORMALIZED)
    return "normalized"
