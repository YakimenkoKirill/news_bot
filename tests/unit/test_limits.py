import pytest

from app.config import PublisherConfig
from app.publisher.formatter import Post
from app.publisher.limits import fit_post, shorten_summary, utf16_len, visible_length
from tests.config_helpers import base_section

CFG = PublisherConfig(**base_section("publisher"))
SMALL = PublisherConfig(
    **{
        **base_section("publisher"),
        "caption_limit": 120,
        "text_limit": 300,
        "min_summary_chars": 40,
    }
)


def _post(summary: str, title: str = "Заголовок", hashtags: list[str] | None = None) -> Post:
    return Post(
        title=title,
        summary=summary,
        link="https://news.example/a?x=1&y=2",
        source_name="Пример",
        hashtags=["#тег"] if hashtags is None else hashtags,
    )


def test_config_matches_bot_api_limits() -> None:
    assert (CFG.caption_limit, CFG.text_limit) == (1024, 4096)


def test_visible_length_ignores_tags_and_unescapes_entities() -> None:
    assert visible_length('<b>A&amp;B</b>\n<a href="https://e.com/?a=1&amp;b=2">ссылка</a>') == len(
        "A&B\nссылка"
    )
    assert visible_length("🚀") == 2


def test_short_post_unchanged() -> None:
    post = _post("Короткий пересказ.")
    fitted = fit_post(post, has_photo=True, cfg=CFG)
    assert fitted.html == post.html
    assert fitted.use_photo is True


def test_cut_by_sentence() -> None:
    summary = "Первое предложение про процессор. Второе предложение про цену. " + "Третье " * 30
    post = _post(summary)
    fitted = fit_post(post, has_photo=True, cfg=SMALL)
    assert visible_length(fitted.html) <= SMALL.caption_limit
    assert "Первое предложение про процессор." in fitted.html
    assert "Третье" not in fitted.html
    assert "<b>Заголовок</b>" in fitted.html
    assert "#тег" in fitted.html
    assert "news.example" not in fitted.html


def test_cut_by_word_with_ellipsis() -> None:
    summary = "слово " * 100
    fitted = fit_post(_post(summary), has_photo=False, cfg=SMALL)
    assert visible_length(fitted.html) <= SMALL.text_limit
    body = fitted.html.split("\n\n")[1]
    assert body.endswith("…")
    assert body[:-1].split()[-1] == "слово"


def test_switch_to_text_when_caption_too_small() -> None:
    long_title = "Длинный заголовок " * 5
    post = _post("Пересказ, которому не хватит места рядом с фото. " * 5, title=long_title)
    fitted = fit_post(post, has_photo=True, cfg=SMALL)
    assert fitted.use_photo is False
    assert visible_length(fitted.html) <= SMALL.text_limit


def test_photo_kept_when_minimal_summary_fits() -> None:
    post = _post("Начало пересказа достаточно длинное. " + "Хвост пересказа " * 20)
    fitted = fit_post(post, has_photo=True, cfg=SMALL)
    assert fitted.use_photo is True
    assert visible_length(fitted.html) <= SMALL.caption_limit


def test_emoji_counted_as_two_units() -> None:
    summary = "🚀" * 200
    fitted = fit_post(_post(summary, hashtags=[]), has_photo=False, cfg=SMALL)
    assert visible_length(fitted.html) <= SMALL.text_limit
    body = fitted.html.split("\n\n")[1]
    assert body.endswith("…")
    assert utf16_len(body) <= SMALL.text_limit


def test_entities_never_cut() -> None:
    summary = "Том & Джерри " * 60
    fitted = fit_post(_post(summary), has_photo=False, cfg=SMALL)
    assert visible_length(fitted.html) <= SMALL.text_limit
    body = fitted.html.split("\n\n")[1]
    assert "&amp" not in body.replace("&amp;", "")
    assert not body.rstrip("…").endswith("&")


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("Одно. Два. Три.", 10, "Одно. Два."),
        ("Одно. Два. Три.", 9, "Одно."),
        ("Одно. Два. Три.", 5, "Одно."),
        ("один два три", 9, "один два…"),
        ("сверхдлинноеслово", 6, "сверх…"),
        ("короткий", 100, "короткий"),
        ("что угодно", 1, ""),
    ],
)
def test_shorten_summary(text: str, limit: int, expected: str) -> None:
    result = shorten_summary(text, limit)
    assert result == expected
    assert utf16_len(result) <= limit


def test_decimal_point_is_not_sentence_end() -> None:
    text = "Рост на 1.5 процента за год и далее текст"
    assert shorten_summary(text, 21) == "Рост на 1.5 процента…"
    assert shorten_summary(text, 20) == "Рост на 1.5…"
