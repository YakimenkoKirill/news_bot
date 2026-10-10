import json
from typing import Any

import pytest

from app.config import ChannelProfile
from app.pipeline.ai.client import AiResponseError
from app.pipeline.ai.schema import AiResult, normalize_hashtags, parse_ai_response
from tests.config_helpers import base_section

PROFILE = ChannelProfile(**{**base_section("channel"), "max_title_chars": 40, "hashtags_max": 3})


def _payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "relevant": True,
        "score": 0.8,
        "title": "Новый процессор вышел",
        "summary": "Компания выпустила процессор, он быстрее прошлого поколения.",
        "hashtags": ["#технологии", "процессоры"],
        "category": "железо",
    }
    data.update(overrides)
    return data


def _parse(data: dict[str, Any] | str) -> AiResult:
    content = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return parse_ai_response(content, PROFILE)


def test_valid_response() -> None:
    result = _parse(_payload())
    assert result == AiResult(
        relevant=True,
        score=0.8,
        title="Новый процессор вышел",
        summary="Компания выпустила процессор, он быстрее прошлого поколения.",
        hashtags=["#технологии", "#процессоры"],
        category="железо",
    )


@pytest.mark.parametrize(
    "wrapper", ["```json\n{body}\n```", "```\n{body}\n```", "  ```JSON {body} ```  "]
)
def test_code_fence_removed(wrapper: str) -> None:
    body = json.dumps(_payload(), ensure_ascii=False)
    assert _parse(wrapper.format(body=body)).title == "Новый процессор вышел"


def test_extra_keys_ignored() -> None:
    assert _parse(_payload(reasoning="подумал", confidence=1)).score == 0.8


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("не json вовсе", "не JSON"),
        ("[1, 2, 3]", "JSON-объект"),
        ('{"relevant": true', "не JSON"),
    ],
)
def test_invalid_json(content: str, reason: str) -> None:
    with pytest.raises(AiResponseError, match=reason):
        _parse(content)


@pytest.mark.parametrize("key", ["relevant", "score", "title", "summary", "hashtags", "category"])
def test_missing_key(key: str) -> None:
    data = _payload()
    del data[key]
    with pytest.raises(AiResponseError, match=key):
        _parse(data)


@pytest.mark.parametrize("score", [-0.1, 1.01, "много"])
def test_score_out_of_range(score: Any) -> None:
    with pytest.raises(AiResponseError, match="score"):
        _parse(_payload(score=score))


@pytest.mark.parametrize("summary", ["", "   "])
def test_empty_summary(summary: str) -> None:
    with pytest.raises(AiResponseError, match="summary"):
        _parse(_payload(summary=summary))


def test_title_too_long() -> None:
    with pytest.raises(AiResponseError, match="title длиннее 40"):
        _parse(_payload(title="Очень длинный заголовок " * 3))


def test_summary_too_long() -> None:
    long_summary = "слово " * PROFILE.max_summary_chars
    with pytest.raises(AiResponseError, match="summary длиннее"):
        _parse(_payload(summary=long_summary))


def test_hashtag_normalization() -> None:
    result = _parse(
        _payload(
            hashtags=[
                "искусственный интеллект",
                "#ИИ",
                "ии",
                "##AI-news",
                "  ",
                "#",
                "лишний",
            ]
        )
    )
    assert result.hashtags == ["#искусственный_интеллект", "#ИИ", "#AI_news"]


def test_normalize_hashtags_limit_zero() -> None:
    assert normalize_hashtags(["a", "b"], 0) == []
    assert normalize_hashtags(["  с  пробелами  "], 5) == ["#с_пробелами"]


def test_hashtags_must_be_list() -> None:
    with pytest.raises(AiResponseError, match="hashtags"):
        _parse(_payload(hashtags="#один #два"))
