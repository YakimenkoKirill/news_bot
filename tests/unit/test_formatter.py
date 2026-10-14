import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from app.publisher.formatter import Post, build_post


@dataclass
class Processed:
    ai_title: str = "Nvidia показала новый процессор"
    ai_summary: str = "Процессор на 30% быстрее прошлой модели."
    hashtags: list[str] = field(default_factory=lambda: ["#железо", "#ИИ"])


@dataclass
class News:
    url: str = "https://news.example/a?id=1&lang=ru"
    image_url: str | None = "https://cdn.example/1.jpg"


@dataclass
class Source:
    name: str = "Пример Новости"


class TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)


def _tags(html_text: str) -> set[str]:
    parser = TagCollector()
    parser.feed(html_text)
    return set(parser.tags)


def test_reference_post() -> None:
    post = build_post(Processed(), News(), Source())
    assert post.html == (
        "<b>Nvidia показала новый процессор</b>\n\n"
        "Процессор на 30% быстрее прошлой модели.\n\n"
        "#железо #ИИ"
    )
    assert post.image_url == "https://cdn.example/1.jpg"
    assert _tags(post.html) == {"b"}


def test_source_not_published() -> None:
    """Ни ссылка, ни имя источника в канал не уходят."""
    post = build_post(Processed(), News(), Source())
    assert "news.example" not in post.html
    assert "Пример Новости" not in post.html
    assert "Источник" not in post.html
    assert "<a" not in post.html
    assert re.findall(r'href="([^"]*)"', post.html) == []


def test_source_kept_in_post_object() -> None:
    """Данные источника сохраняются в объекте поста, хотя и не публикуются."""
    post = build_post(Processed(), News(), Source())
    assert post.link == "https://news.example/a?id=1&lang=ru"
    assert post.source_name == "Пример Новости"


def test_parts_kept_separately() -> None:
    post = build_post(Processed(), News(), Source())
    assert post.title == "Nvidia показала новый процессор"
    assert post.summary == "Процессор на 30% быстрее прошлой модели."
    assert post.hashtags == ["#железо", "#ИИ"]


def test_special_characters_escaped() -> None:
    processed = Processed(
        ai_title='<script>alert("x")</script> & «кавычки»',
        ai_summary="a < b > c & 'd' \"e\"",
    )
    post = build_post(processed, News(), Source(name="Tom & Jerry <News>"))
    assert "<script>" not in post.html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; «кавычки»" in post.html
    assert "a &lt; b &gt; c &amp; &#x27;d&#x27; &quot;e&quot;" in post.html
    assert _tags(post.html) == {"b"}


def test_no_hashtags_line_omitted() -> None:
    post = build_post(Processed(hashtags=[]), News(), Source())
    assert post.html == (
        "<b>Nvidia показала новый процессор</b>\n\nПроцессор на 30% быстрее прошлой модели."
    )
    assert "\n\n\n" not in post.html


def test_emoji_preserved() -> None:
    processed = Processed(ai_title="🚀 Запуск", ai_summary="Ракета 🔥 взлетела")
    post = build_post(processed, News(), Source())
    assert "<b>🚀 Запуск</b>" in post.html
    assert "Ракета 🔥 взлетела" in post.html


def test_visible_length_counts_text_without_markup() -> None:
    post = Post(title="T&", summary="S", link="https://e.com/?a=1&b=2", source_name="Src")
    assert post.visible_length == len("T&\n\nS")
    assert post.with_summary("Другой").summary == "Другой"
    assert post.with_summary("Другой").title == "T&"
