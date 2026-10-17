"""Проверка, что цифры и имена из пересказа есть в источнике."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

NAME_PREFIX_LETTERS = 5
FOREIGN_NOTE = "перевод из {lang}: имена не проверены автоматически"

_NUMBER = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:[,   ]\d{3})+|\d+)(?:[.,]\d+)?(?![\w])\s?%?")
_THOUSANDS_GROUPS = re.compile(r"^\d{1,3}(?:,\d{3})+$")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё][\w-]*")
_SENTENCE_END = frozenset(".!?…\n")
_OPENERS = frozenset("«\"'([“„ \t -—–")


def normalize_number(raw: str) -> str:
    """Каноническая запись числа: без разделителей разрядов и %, точка — дробная часть.

    «1 000», «1,000» и «1000» дают «1000»; «1,5» и «1.50» — «1.5»;
    «1,000.5» — «1000.5». Запятая считается разделителем разрядов, только если за
    ней идут группы по три цифры, иначе это дробная часть.

    Args:
        raw: Число, как оно записано в тексте.

    Returns:
        Нормализованная запись; неразобранное число возвращается как есть.
    """
    compact = re.sub(r"[\s\u00a0\u202f%]", "", raw)
    if ("." in compact and "," in compact) or _THOUSANDS_GROUPS.match(compact):
        compact = compact.replace(",", "")
    else:
        compact = compact.replace(",", ".")
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


def is_foreign(source_lang: str | None, channel_lang: str | None) -> bool:
    """Отличается ли язык источника от языка канала.

    Неизвестный язык считается языком канала: так ведёт себя и нормализация,
    когда определить язык не удалось.

    Args:
        source_lang: Язык источника (news_items.language).
        channel_lang: Язык канала (channel.language).

    Returns:
        True, если пересказ — это перевод.
    """
    if not source_lang or not channel_lang:
        return False
    return source_lang.strip().lower() != channel_lang.strip().lower()


def unsupported_facts(
    source_text: str,
    summary: str,
    source_lang: str | None = None,
    channel_lang: str | None = None,
) -> list[str]:
    """Возвращает числа и имена из пересказа, которых нет в источнике.

    Число подтверждено, если в источнике есть то же число после нормализации
    записи. Аббревиатура (ЕС, NASA) — если она встречается в источнике целиком.
    Имя (слово с заглавной буквы не в начале предложения) — если в источнике есть
    слово с тем же началом из 5 букв, что покрывает падежи.

    У перевода (язык источника не совпадает с языком канала) имена не проверяются:
    в пересказе они написаны на другом языке, и сравнение по началу слова дало бы
    ложные замечания. Числа проверяются всегда — записи вида «1,000» и «1 000»
    приводятся к одному виду.

    Args:
        source_text: Текст источника.
        summary: Пересказ модели.
        source_lang: Язык источника; None — считается языком канала.
        channel_lang: Язык канала; None — считается совпадающим.

    Returns:
        Неподтверждённые числа и имена в порядке появления, без повторов.
    """
    if not summary.strip():
        return []
    numbers_only = is_foreign(source_lang, channel_lang)
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

    if numbers_only:
        return problems

    for word in _capitalized_words(summary):
        if _is_abbreviation(word):
            if word not in source_exact:
                add(word)
            continue
        prefix = _normalize_word(word)[:NAME_PREFIX_LETTERS]
        if not any(candidate.startswith(prefix) for candidate in source_lower):
            add(word)
    return problems
