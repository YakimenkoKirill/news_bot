import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.pipeline.ai.prompts import (
    LEGACY_PROMPT_VERSION,
    LEGACY_RESPONSE_KEYS,
    PROMPT_VERSION,
    RESPONSE_KEYS,
    SCREEN_RESPONSE_KEYS,
    ChannelProfile,
    build_legacy_messages,
    build_messages,
    build_screen_messages,
    truncate_words,
)
from tests.config_helpers import base_section

PROFILE = ChannelProfile(**base_section("channel"))
MAX_INPUT = base_section("ai")["max_input_chars"]
SCREEN_INPUT = base_section("ai")["screen_input_chars"]
SECRET_TEXT = "УНИКАЛЬНЫЙ-ТЕКСТ-НОВОСТИ про запуск спутника"


@dataclass
class News:
    title: str = "Запуск спутника перенесли"
    url: str = "https://www.space.example/news/42"
    clean_text: str | None = SECRET_TEXT
    raw_text: str = "<p>сырой</p>"
    language: str | None = "ru"
    published_at: datetime | None = datetime(2026, 10, 10, 9, 30, tzinfo=UTC)


def _messages(news: News | None = None, max_input: int = MAX_INPUT) -> list[dict[str, str]]:
    return build_messages(news or News(), PROFILE, max_input)


def _screen_messages(
    news: News | None = None, max_chars: int = SCREEN_INPUT
) -> list[dict[str, str]]:
    return build_screen_messages(news or News(), PROFILE, max_chars)


def test_version_is_string() -> None:
    assert isinstance(PROMPT_VERSION, str)
    assert PROMPT_VERSION
    assert PROMPT_VERSION == "v5"
    assert LEGACY_PROMPT_VERSION == "v3"


def test_summary_prompt_without_relevance_keys() -> None:
    """Оценку даёт отбор: промпт пересказа про неё не спрашивает."""
    assert RESPONSE_KEYS == ("title", "summary", "hashtags", "category")
    system = _messages()[0]["content"]
    for key in SCREEN_RESPONSE_KEYS:
        assert f'"{key}"' not in system
    assert "Оцени" not in system


def test_screen_prompt_asks_only_for_relevance() -> None:
    messages = _screen_messages()
    assert [m["role"] for m in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]
    assert SCREEN_RESPONSE_KEYS == ("relevant", "score")
    for key in SCREEN_RESPONSE_KEYS:
        assert f'"{key}"' in system
    for key in RESPONSE_KEYS:
        assert f'"{key}"' not in system
    assert PROFILE.topic in system
    assert "JSON" in system
    assert "Пересказывать новость не нужно" in system
    assert SECRET_TEXT in user
    assert "Заголовок: Запуск спутника перенесли" in user


def test_screen_system_message_has_no_changing_data() -> None:
    """Системное сообщение отбора одинаково для всех новостей — работает кэш."""
    first = _screen_messages()[0]["content"]
    second = _screen_messages(
        News(title="Другая новость", clean_text="другой текст", published_at=None)
    )[0]["content"]
    assert first == second
    assert "2026" not in first
    assert "Дата" not in first


def test_screen_user_message_has_no_date() -> None:
    user = _screen_messages()[1]["content"]
    assert "Дата публикации" not in user


def test_screen_text_truncated_to_screen_input_chars() -> None:
    long_text = "слово " * 2000
    user = _screen_messages(News(clean_text=long_text), max_chars=80)[1]["content"]
    text_part = user.split("Текст:\n", 1)[1]
    assert len(text_part) <= 80
    assert text_part.endswith("…")


def test_screen_text_uses_raw_text_fallback() -> None:
    user = _screen_messages(News(clean_text=None))[1]["content"]
    assert "<p>сырой</p>" in user


def test_legacy_prompt_keeps_all_keys() -> None:
    system = build_legacy_messages(News(), PROFILE, MAX_INPUT)[0]["content"]
    assert (*SCREEN_RESPONSE_KEYS, *RESPONSE_KEYS) == LEGACY_RESPONSE_KEYS
    for key in LEGACY_RESPONSE_KEYS:
        assert f'"{key}"' in system
    assert "Оцени" in system
    assert "Дата публикации" in build_legacy_messages(News(), PROFILE, MAX_INPUT)[1]["content"]


def test_screen_messages_are_json_serializable() -> None:
    assert json.loads(json.dumps(_screen_messages())) == _screen_messages()


def test_system_and_user_split() -> None:
    messages = _messages()
    assert [m["role"] for m in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]
    assert SECRET_TEXT not in system
    assert "Запуск спутника перенесли" not in system
    assert SECRET_TEXT in user
    assert "Заголовок: Запуск спутника перенесли" in user
    assert "space.example" not in user
    assert "Дата публикации: 2026-10-10T09:30:00+00:00" in user


def test_system_contains_profile_rules_and_keys() -> None:
    system = _messages()[0]["content"]
    assert PROFILE.topic in system
    assert PROFILE.tone in system
    assert PROFILE.language in system
    for key in RESPONSE_KEYS:
        assert f'"{key}"' in system
    assert str(PROFILE.max_title_chars) in system
    assert str(PROFILE.max_summary_chars) in system
    assert str(PROFILE.hashtags_max) in system
    assert "Не добавляй факты" in system
    assert "15 слов" in system
    assert "JSON" in system


def test_user_message_has_source_language() -> None:
    user = _messages()[1]["content"]
    assert "Язык источника: ru" in user
    screen = _screen_messages()[1]["content"]
    assert "Язык источника: ru" in screen


def test_foreign_language_passed_as_is() -> None:
    user = _messages(News(language="en"))[1]["content"]
    assert "Язык источника: en" in user


def test_unknown_language_marked() -> None:
    """Язык не определён — модель узнаёт об этом явно."""
    user = _messages(News(language=None))[1]["content"]
    assert "Язык источника: не определён" in user


def test_translation_rule_in_summary_prompt() -> None:
    system = _messages()[0]["content"]
    assert "Если язык источника отличается от языка канала" in system
    assert PROFILE.language in system
    assert "в оригинале в скобках" in system


def test_translation_rule_in_legacy_prompt() -> None:
    system = build_legacy_messages(News(), PROFILE, MAX_INPUT)[0]["content"]
    assert "Если язык источника отличается от языка канала" in system


def test_screen_prompt_has_no_translation_rule() -> None:
    """Отбор только оценивает тему: пересказывать и переводить его не просят."""
    system = _screen_messages()[0]["content"]
    assert "в оригинале в скобках" not in system
    assert "перескажи" not in system.lower()


def test_missing_date_and_raw_text_fallback() -> None:
    user = _messages(News(clean_text=None, published_at=None))[1]["content"]
    assert "Дата публикации: не указана" in user
    assert "<p>сырой</p>" in user


def test_text_truncated_to_max_input() -> None:
    long_text = "слово " * 2000
    user = _messages(News(clean_text=long_text), max_input=100)[1]["content"]
    text_part = user.split("Текст:\n", 1)[1]
    assert len(text_part) <= 100
    assert text_part.endswith("…")
    assert text_part[:-1].split()[-1] == "слово"


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("короткий текст", 50, "короткий текст"),
        ("один два три четыре", 12, "один два…"),
        ("один два три", 9, "один два…"),
        ("сверхдлинноеслово", 6, "сверх…"),
    ],
)
def test_truncate_words(text: str, limit: int, expected: str) -> None:
    result = truncate_words(text, limit)
    assert result == expected
    assert len(result) <= limit


def test_profile_requires_all_fields() -> None:
    fields = base_section("channel")
    fields.pop("tone")
    with pytest.raises(ValidationError, match="tone"):
        ChannelProfile(**fields)


def test_messages_are_json_serializable() -> None:
    assert json.loads(json.dumps(_messages())) == _messages()
