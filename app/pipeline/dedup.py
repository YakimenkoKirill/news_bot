"""Дедупликация: SimHash заголовка и начала текста."""

from __future__ import annotations

from simhash import Simhash

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
