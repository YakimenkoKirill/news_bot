"""Проверка, что цифры и имена из пересказа есть в источнике."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

NAME_PREFIX_LETTERS = 5

_NUMBER = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,]\d+)?(?![\w])\s?%?")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё][\w-]*")
_SENTENCE_END = frozenset(".!?…\n")
_OPENERS = frozenset("«\"'([“„ \t -—–")


def normalize_number(raw: str) -> str:
    """Каноническая запись числа: без пробелов-разделителей и %, точка вместо запятой.

    «1 000» и «1000» дают «1000», «1,5» и «1.50» — «1.5».
    """
    compact = re.sub(r"[\s  %]", "", raw).replace(",", ".")
    try:
        value = Decimal(compact)
    except InvalidOperation:
        return compact
    return format(value.normalize(), "f")


def _numbers(text: str) -> list[str]:
    return [m.group(0).strip() for m in _NUMBER.finditer(text)]


def _is_sentence_start(text: str, start: int) -> bool:
    position = start - 1
    while position >= 0 and text[position] in _OPENERS:
        position -= 1
    return position < 0 or text[position] in _SENTENCE_END


def _normalize_word(word: str) -> str:
    return word.lower().replace("ё", "е")


def _capitalized_words(text: str) -> list[str]:
    words: list[str] = []
    for match in _WORD.finditer(text):
        word = match.group(0)
        if word[0].isupper() and not _is_sentence_start(text, match.start()):
            words.append(word)
    return words


def _is_abbreviation(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    return len(letters) > 1 and all(c.isupper() for c in letters)


def unsupported_facts(source_text: str, summary: str) -> list[str]:
    """Возвращает числа и имена из пересказа, которых нет в источнике.

    Число подтверждено, если в источнике есть то же число после нормализации
    записи. Аббревиатура (ЕС, NASA) — если она встречается в источнике целиком.
    Имя (слово с заглавной буквы не в начале предложения) — если в источнике есть
    слово с тем же началом из 5 букв, что покрывает падежи.

    Args:
        source_text: Текст источника.
        summary: Пересказ модели.

    Returns:
        Неподтверждённые числа и имена в порядке появления, без повторов.
    """
    if not summary.strip():
        return []
    source_numbers = {normalize_number(n) for n in _numbers(source_text)}
    source_words = [m.group(0) for m in _WORD.finditer(source_text)]
    source_exact = set(source_words)
    source_lower = [_normalize_word(w) for w in source_words]

    problems: list[str] = []

    def add(item: str) -> None:
        if item not in problems:
            problems.append(item)

    for number in _numbers(summary):
        if normalize_number(number) not in source_numbers:
            add(number)

    for word in _capitalized_words(summary):
        if _is_abbreviation(word):
            if word not in source_exact:
                add(word)
            continue
        prefix = _normalize_word(word)[:NAME_PREFIX_LETTERS]
        if not any(candidate.startswith(prefix) for candidate in source_lower):
            add(word)
    return problems
