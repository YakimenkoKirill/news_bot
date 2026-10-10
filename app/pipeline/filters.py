"""Фильтры новостей: возраст, язык, стоп-слова и обязательные ключевые слова."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from app.config import FiltersConfig
from app.db.repositories.misc import normalize_keyword
from app.pipeline.normalizer import LANG_UNKNOWN

REASON_TOO_OLD = "too_old"
REASON_LANGUAGE = "language"
REASON_STOP_WORD = "stop_word"
REASON_NO_KEYWORD = "no_keyword"

_WORD_GAP = r"\s+"


def _normalize_text(text: str) -> str:
    return text.lower().replace("ё", "е")


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Фраза по границам слов; слова фразы разделены любыми пробелами."""
    body = _WORD_GAP.join(re.escape(word) for word in phrase.split())
    return re.compile(rf"(?<!\w){body}(?!\w)")


@dataclass(frozen=True)
class KeywordSets:
    """Обязательные (include) и стоп-слова (exclude); слова и фразы."""

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    _include: list[tuple[str, re.Pattern[str]]] = field(init=False, repr=False, compare=False)
    _exclude: list[tuple[str, re.Pattern[str]]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_include", self._compile(self.include))
        object.__setattr__(self, "_exclude", self._compile(self.exclude))

    @staticmethod
    def _compile(words: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
        compiled: list[tuple[str, re.Pattern[str]]] = []
        for word in words:
            if word.strip():
                normalized = normalize_keyword(word)
                compiled.append((normalized, _phrase_pattern(normalized)))
        return compiled

    def find_stop_word(self, text: str) -> str | None:
        """Первое найденное стоп-слово (в нормализованном виде) или None."""
        return next((word for word, pattern in self._exclude if pattern.search(text)), None)

    def has_include(self) -> bool:
        """Задан ли непустой список обязательных слов."""
        return bool(self._include)

    def matches_include(self, text: str) -> bool:
        """Есть ли в тексте хотя бы одно обязательное слово."""
        return any(pattern.search(text) for _, pattern in self._include)


class FilterableNews(Protocol):
    """Поля новости, нужные фильтрам (NewsItem подходит структурно)."""

    title: str
    clean_text: str | None
    raw_text: str
    language: str | None
    published_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class FilterDecision:
    """Решение фильтров: прошла ли новость и причина отказа."""

    passed: bool
    reason: str | None = None


PASSED = FilterDecision(passed=True)


def apply_filters(
    item: FilterableNews, now: datetime, keywords: KeywordSets, cfg: FiltersConfig
) -> FilterDecision:
    """Проверяет новость фильтрами по порядку.

    Порядок: возраст (published_at, иначе created_at) -> язык -> стоп-слова в
    заголовке или тексте -> обязательные слова (если список include не пуст).
    Поиск слов — по границам слов, без учёта регистра, ё = е.

    Returns:
        FilterDecision с причиной: too_old, language:<код>, stop_word:<слово> или no_keyword.
    """
    reference = item.published_at or item.created_at
    if reference is not None and now - reference > timedelta(hours=cfg.max_age_h):
        return FilterDecision(passed=False, reason=REASON_TOO_OLD)

    language = item.language or LANG_UNKNOWN
    if language not in cfg.allowed_languages:
        return FilterDecision(passed=False, reason=f"{REASON_LANGUAGE}:{language}")

    text = _normalize_text(f"{item.title}\n{item.clean_text or item.raw_text}")
    stop_word = keywords.find_stop_word(text)
    if stop_word is not None:
        return FilterDecision(passed=False, reason=f"{REASON_STOP_WORD}:{stop_word}")

    if keywords.has_include() and not keywords.matches_include(text):
        return FilterDecision(passed=False, reason=REASON_NO_KEYWORD)
    return PASSED
