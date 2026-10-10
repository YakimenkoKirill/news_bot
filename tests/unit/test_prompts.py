import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.pipeline.ai.prompts import (
    PROMPT_VERSION,
    RESPONSE_KEYS,
    ChannelProfile,
    build_messages,
    truncate_words,
)
from tests.config_helpers import base_section

PROFILE = ChannelProfile(**base_section("channel"))
MAX_INPUT = base_section("ai")["max_input_chars"]
SECRET_TEXT = "УНИКАЛЬНЫЙ-ТЕКСТ-НОВОСТИ про запуск спутника"


@dataclass
class News:
    title: str = "Запуск спутника перенесли"
    url: str = "https://www.space.example/news/42"
    clean_text: str | None = SECRET_TEXT
    raw_text: str = "<p>сырой</p>"
    published_at: datetime | None = datetime(2026, 10, 10, 9, 30, tzinfo=UTC)


def _messages(news: News | None = None, max_input: int = MAX_INPUT) -> list[dict[str, str]]:
    return build_messages(news or News(), PROFILE, max_input)


def test_version_is_string() -> None:
    assert isinstance(PROMPT_VERSION, str)
    assert PROMPT_VERSION


def test_system_and_user_split() -> None:
    messages = _messages()
    assert [m["role"] for m in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]
    assert SECRET_TEXT not in system
    assert "Запуск спутника перенесли" not in system
    assert SECRET_TEXT in user
    assert "Заголовок: Запуск спутника перенесли" in user
    assert "Источник: space.example" in user
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
