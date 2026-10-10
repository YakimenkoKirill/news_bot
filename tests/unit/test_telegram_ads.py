from types import SimpleNamespace
from typing import Any

import pytest

from app.collectors.telegram.ads import AdRules, is_advertisement
from tests.config_helpers import telegram_config

RULES = AdRules.from_config(telegram_config())


def _msg(text: str | None = "", fwd_from: Any = None) -> SimpleNamespace:
    return SimpleNamespace(message=text, fwd_from=fwd_from)


def test_rules_from_config() -> None:
    assert RULES.markers == ["#реклама", "erid"]
    assert RULES.skip_forwards is True


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("Лучшие курсы! #реклама", "#реклама"),
        ("Скидки до 50%\nerid: 2VtzqwXyZ", "erid"),
        ("ERID 2VtzqwXyZ — партнёрский материал", "erid"),
        ("#Реклама в начале", "#реклама"),
        ("(erid) в скобках", "erid"),
    ],
)
def test_each_marker_case_insensitive(text: str, marker: str) -> None:
    assert is_advertisement(_msg(text), RULES) == f"ad_marker:{marker}"


@pytest.mark.parametrize(
    "text",
    [
        "Слово veridical содержит маркер внутри",
        "#рекламации по качеству",
        "Тег #рекламабесплатно",
        "erid2 без границы",
    ],
)
def test_marker_inside_word_ignored(text: str) -> None:
    assert is_advertisement(_msg(text), RULES) is None


def test_forwarded_skipped_with_flag() -> None:
    assert is_advertisement(_msg("новость", fwd_from=object()), RULES) == "forwarded"


def test_forwarded_allowed_without_flag() -> None:
    rules = AdRules.from_config(telegram_config(skip_forwards=False))
    assert is_advertisement(_msg("новость", fwd_from=object()), rules) is None
    assert is_advertisement(_msg("#реклама", fwd_from=object()), rules) == "ad_marker:#реклама"


def test_regular_message_passes() -> None:
    assert is_advertisement(_msg("Обычная новость про технологии"), RULES) is None


@pytest.mark.parametrize("text", ["", None])
def test_empty_text_is_not_error(text: str | None) -> None:
    assert is_advertisement(_msg(text), RULES) is None


def test_blank_markers_ignored() -> None:
    rules = AdRules(markers=["", "  ", "спонсор"], skip_forwards=False)
    assert is_advertisement(_msg("наш спонсор"), rules) == "ad_marker:спонсор"
    assert is_advertisement(_msg("обычный текст"), rules) is None
