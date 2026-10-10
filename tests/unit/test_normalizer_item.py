import hashlib
from dataclasses import dataclass

import pytest

from app.db.models import NewsItem
from app.pipeline.normalizer import (
    NormalizedFields,
    content_hash,
    detect_language,
    normalize_item,
)
from tests.config_helpers import normalize_config

CONFIG = normalize_config()
RATIO = CONFIG.lang_ratio


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Компания представила новый процессор для ноутбуков", "ru"),
        ("Apple unveiled a new processor for laptops", "en"),
        ("Компания Apple представила процессор M5", "ru"),
        ("Google и Microsoft выпустили обновления", "unknown"),
        ("Google и Microsoft выпустили крупные обновления систем", "ru"),
        ("OpenAI GPT model API привет", "en"),
        ("Привет hello мир world", "unknown"),
        ("12345 !!! 🚀 — 2026", "unknown"),
        ("", "unknown"),
    ],
)
def test_detect_language(text: str, expected: str) -> None:
    assert detect_language(text, RATIO) == expected


def test_threshold_controls_mixed_text() -> None:
    text = "один два three"
    assert detect_language(text, 0.9) == "unknown"
    assert detect_language(text, 0.5) == "ru"


def test_hash_ignores_case_punctuation_spaces_and_yo() -> None:
    variants = [
        "Ёжик вышел из тумана. Что дальше?",
        "ёжик вышел   из тумана что дальше",
        "ЕЖИК, вышел из тумана!!! Что — дальше…",
        "\n  Ежик вышел из «тумана»;\tчто дальше  ",
    ]
    hashes = {content_hash(v) for v in variants}
    assert len(hashes) == 1


def test_hash_differs_for_different_texts() -> None:
    assert content_hash("Курс доллара вырос") != content_hash("Курс доллара упал")
    assert content_hash("цена 100") != content_hash("цена 1000")


def test_hash_is_stable_sha256() -> None:
    expected = hashlib.sha256("привет мир".encode()).hexdigest()
    assert content_hash("Привет, мир!") == expected
    assert len(content_hash("x")) == 64


@dataclass
class FakeNews:
    url: str
    title: str
    raw_text: str


def test_normalize_item() -> None:
    news = NewsItem(
        source_id=1,
        external_id="1",
        url="https://Example.com/news/42/?utm_source=tg#top",
        title="Новый процессор",
        raw_text="<p>Компания&nbsp;представила <b>процессор</b>.</p><p>Второй абзац.</p>",
    )
    fields = normalize_item(news, CONFIG)
    assert fields == NormalizedFields(
        clean_text="Компания представила процессор.\n\nВторой абзац.",
        canonical_url="https://example.com/news/42",
        language="ru",
        content_hash=content_hash("Новый процессор\nКомпания представила процессор. Второй абзац."),
    )


def test_photo_only_posts_do_not_collide() -> None:
    first = normalize_item(FakeNews("https://t.me/ch/1", "", ""), CONFIG)
    second = normalize_item(FakeNews("https://t.me/ch/2", "Подпись", ""), CONFIG)
    assert first.clean_text == ""
    assert first.language == "unknown"
    assert first.content_hash != second.content_hash


def test_normalize_item_invalid_url() -> None:
    with pytest.raises(ValueError):
        normalize_item(FakeNews("not-a-url", "t", "text"), CONFIG)
