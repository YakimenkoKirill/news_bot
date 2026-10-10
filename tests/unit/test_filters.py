from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.config import FiltersConfig
from app.pipeline.filters import FilterDecision, KeywordSets, apply_filters
from tests.config_helpers import base_section

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CFG = FiltersConfig(**{**base_section("filters"), "allowed_languages": ["ru"]})
NO_KEYWORDS = KeywordSets()


@dataclass
class Item:
    title: str = "Компания представила новый процессор"
    clean_text: str | None = "Новинка быстрее предыдущего поколения."
    raw_text: str = ""
    language: str | None = "ru"
    published_at: datetime | None = NOW - timedelta(hours=1)
    created_at: datetime = NOW - timedelta(minutes=30)


def _check(item: Item, keywords: KeywordSets = NO_KEYWORDS) -> FilterDecision:
    return apply_filters(item, NOW, keywords, CFG)


def test_config_from_base_yaml() -> None:
    assert CFG.max_age_h == 24


def test_passes_by_default() -> None:
    assert _check(Item()) == FilterDecision(passed=True, reason=None)


def test_too_old() -> None:
    old = Item(published_at=NOW - timedelta(hours=CFG.max_age_h, seconds=1))
    assert _check(old) == FilterDecision(passed=False, reason="too_old")
    boundary = Item(published_at=NOW - timedelta(hours=CFG.max_age_h))
    assert _check(boundary).passed


def test_missing_published_at_uses_created_at() -> None:
    assert _check(Item(published_at=None, created_at=NOW - timedelta(days=2))).reason == "too_old"
    assert _check(Item(published_at=None, created_at=NOW - timedelta(hours=1))).passed


@pytest.mark.parametrize(
    ("language", "reason"), [("en", "language:en"), (None, "language:unknown")]
)
def test_language(language: str | None, reason: str) -> None:
    assert _check(Item(language=language)).reason == reason


def test_stop_word_in_title_or_text() -> None:
    keywords = KeywordSets(exclude=["Казино"])
    assert _check(Item(title="Открылось казино"), keywords).reason == "stop_word:казино"
    assert _check(Item(clean_text="Реклама КАЗИНО тут"), keywords).reason == "stop_word:казино"


def test_no_keyword() -> None:
    keywords = KeywordSets(include=["нейросеть", "робот"])
    assert _check(Item(), keywords).reason == "no_keyword"
    assert _check(Item(clean_text="Новый робот-пылесос"), keywords).passed


def test_check_order() -> None:
    keywords = KeywordSets(include=["нейросеть"], exclude=["процессор"])
    old_foreign = Item(published_at=NOW - timedelta(days=3), language="en")
    assert _check(old_foreign, keywords).reason == "too_old"
    foreign = Item(language="en")
    assert _check(foreign, keywords).reason == "language:en"
    assert _check(Item(), keywords).reason == "stop_word:процессор"
    stop_and_keyword = Item(clean_text="процессор и нейросеть")
    assert _check(stop_and_keyword, keywords).reason == "stop_word:процессор"


def test_word_boundaries() -> None:
    keywords = KeywordSets(exclude=["кот"], include=["ии"])
    assert _check(Item(clean_text="Котировки акций и ИИ"), keywords).passed
    assert _check(Item(clean_text="Наш кот и ИИ"), keywords).reason == "stop_word:кот"
    assert _check(Item(clean_text="Линии метро"), keywords).reason == "no_keyword"


def test_yo_equals_e() -> None:
    assert _check(Item(clean_text="Ежик в тумане"), KeywordSets(exclude=["ёжик"])).reason == (
        "stop_word:ежик"
    )
    assert _check(Item(clean_text="Ёлка у дома"), KeywordSets(include=["елка"])).passed


def test_phrases() -> None:
    keywords = KeywordSets(include=["искусственный интеллект"], exclude=["ставки на спорт"])
    assert _check(Item(clean_text="Искусственный\n  интеллект помог врачам"), keywords).passed
    assert _check(Item(clean_text="Интеллект искусственный"), keywords).reason == "no_keyword"
    assert _check(Item(clean_text="Ставки на спорт растут"), keywords).reason == (
        "stop_word:ставки на спорт"
    )


def test_empty_lists_do_not_filter() -> None:
    assert _check(Item(), KeywordSets(include=[], exclude=[])).passed
    assert _check(Item(), KeywordSets(include=["  "], exclude=[""])).passed


def test_raw_text_used_when_clean_text_missing() -> None:
    keywords = KeywordSets(exclude=["казино"])
    assert _check(Item(clean_text=None, raw_text="казино"), keywords).reason == "stop_word:казино"
