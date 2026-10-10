"""Дедупликация: SimHash заголовка и начала текста, поиск дублей по трём уровням."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from simhash import Simhash

from app.config import DedupConfig
from app.pipeline.normalizer import normalize_for_hash

SIMHASH_BITS = 64
SHINGLE_WORDS = 3
_UNSIGNED_LIMIT = 1 << SIMHASH_BITS
_SIGNED_MAX = (1 << (SIMHASH_BITS - 1)) - 1


def shingles(text: str, size: int = SHINGLE_WORDS) -> list[str]:
    """Шинглы из size слов; если слов меньше size — сами слова."""
    words = normalize_for_hash(text).split()
    if len(words) < size:
        return words
    return [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]


def compute_simhash(title: str, text: str, first_paragraph_chars: int) -> int:
    """64-битный SimHash по шинглам из 3 слов от заголовка и начала текста.

    Args:
        title: Заголовок.
        text: Чистый текст новости.
        first_paragraph_chars: Сколько первых символов текста учитывать
            (dedup.first_paragraph_chars).

    Returns:
        Беззнаковое 64-битное значение.
    """
    features = shingles(f"{title} {text[:first_paragraph_chars]}")
    return int(Simhash(features, f=SIMHASH_BITS).value)


def hamming(a: int, b: int) -> int:
    """Расстояние Хэмминга между двумя 64-битными значениями (в любой форме записи)."""
    return ((a ^ b) % _UNSIGNED_LIMIT).bit_count()


def to_db(value: int) -> int:
    """Беззнаковое 64-битное число -> BIGINT со знаком.

    Raises:
        ValueError: Значение вне диапазона [0, 2**64).
    """
    if not 0 <= value < _UNSIGNED_LIMIT:
        raise ValueError(f"simhash вне 64 бит: {value}")
    return value - _UNSIGNED_LIMIT if value > _SIGNED_MAX else value


def from_db(value: int) -> int:
    """BIGINT со знаком из БД -> беззнаковое 64-битное число."""
    return value + _UNSIGNED_LIMIT if value < 0 else value


class DedupLevel(StrEnum):
    """Уровень, на котором найден дубль."""

    URL = "url"
    HASH = "hash"
    SIMHASH = "simhash"


@dataclass(frozen=True)
class DedupResult:
    """Результат проверки на дубли.

    Attributes:
        is_duplicate: Найден ли дубль.
        duplicate_of: Id найденной новости.
        cluster_id: Кластер новости: кластер найденной (или её id); у уникальной — свой id.
        level: Уровень, на котором найден дубль.
    """

    is_duplicate: bool
    duplicate_of: int | None
    cluster_id: int
    level: DedupLevel | None


class DedupFields(Protocol):
    """Нормализованные поля проверяемой новости (NormalizedFields подходит)."""

    @property
    def canonical_url(self) -> str:
        """Канонический URL."""
        raise NotImplementedError

    @property
    def content_hash(self) -> str:
        """Хеш содержимого."""
        raise NotImplementedError


class FoundNews(Protocol):
    """Найденная новость: id и кластер."""

    id: int
    cluster_id: int | None


class DedupRepo(Protocol):
    """Нужная дедупликации часть NewsRepository."""

    async def find_by_url(
        self, canonical_url: str, since: datetime, exclude_id: int | None = None
    ) -> FoundNews | None:
        """Новость с тем же каноническим URL в окне."""
        raise NotImplementedError

    async def find_by_hash(
        self, content_hash: str, since: datetime, exclude_id: int | None = None
    ) -> FoundNews | None:
        """Новость с тем же хешем в окне."""
        raise NotImplementedError

    async def recent_simhashes(self, since: datetime) -> list[tuple[int, int, int | None]]:
        """(id, simhash, cluster_id) новостей в окне."""
        raise NotImplementedError


class Deduplicator:
    """Трёхуровневый поиск дублей: канонический URL -> хеш содержимого -> SimHash."""

    def __init__(self, repo: DedupRepo, cfg: DedupConfig) -> None:
        self._repo = repo
        self._window = timedelta(hours=cfg.window_h)
        self._max_distance = cfg.simhash_max_distance

    async def check(
        self, item_id: int, fields: DedupFields, simhash: int, now: datetime
    ) -> DedupResult:
        """Ищет дубль новости в окне window_h.

        Уровни проверяются по порядку, срабатывает первый. Для SimHash берётся
        ближайший кандидат с расстоянием <= simhash_max_distance, при равенстве —
        более ранний. Новость не сравнивается сама с собой.

        Args:
            item_id: Id проверяемой новости.
            fields: Её нормализованные поля.
            simhash: Её SimHash (беззнаковый или в форме BIGINT).
            now: Текущий момент.

        Returns:
            Результат; у уникальной новости cluster_id = item_id.
        """
        since = now - self._window
        found = await self._repo.find_by_url(fields.canonical_url, since, exclude_id=item_id)
        if found is not None:
            return self._duplicate(found.id, found.cluster_id, DedupLevel.URL)

        found = await self._repo.find_by_hash(fields.content_hash, since, exclude_id=item_id)
        if found is not None:
            return self._duplicate(found.id, found.cluster_id, DedupLevel.HASH)

        best: tuple[int, int, int | None] | None = None
        best_distance = self._max_distance + 1
        for other_id, other_hash, other_cluster in await self._repo.recent_simhashes(since):
            if other_id == item_id:
                continue
            distance = hamming(simhash, other_hash)
            if distance < best_distance or (
                distance == best_distance and best is not None and other_id < best[0]
            ):
                best, best_distance = (other_id, other_hash, other_cluster), distance
        if best is not None:
            return self._duplicate(best[0], best[2], DedupLevel.SIMHASH)
        return DedupResult(is_duplicate=False, duplicate_of=None, cluster_id=item_id, level=None)

    @staticmethod
    def _duplicate(found_id: int, found_cluster: int | None, level: DedupLevel) -> DedupResult:
        return DedupResult(
            is_duplicate=True,
            duplicate_of=found_id,
            cluster_id=found_cluster if found_cluster is not None else found_id,
            level=level,
        )
