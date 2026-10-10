import pytest

from app.pipeline.normalizer import canonical_url, clean_html
from tests.config_helpers import base_section

DROP = base_section("normalize")["drop_query_params"]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("HTTPS://Example.COM/News/1", "https://example.com/News/1"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com/a#comments", "https://example.com/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/a///", "https://example.com/a"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com", "https://example.com/"),
        (
            "https://example.com/a?utm_source=tg&utm_medium=post&id=5",
            "https://example.com/a?id=5",
        ),
        ("https://example.com/a?b=2&a=1&c=3", "https://example.com/a?a=1&b=2&c=3"),
        ("https://example.com/a?fbclid=X&gclid=Y&yclid=Z", "https://example.com/a"),
        ("https://example.com/a?UTM_Campaign=x&page=2", "https://example.com/a?page=2"),
        ("https://example.com/a?q=&x=1", "https://example.com/a?q=&x=1"),
        (
            "https://example.com/search?q=hello%20world",
            "https://example.com/search?q=hello%20world",
        ),
        ("https://Пример.РФ/Новость", "https://пример.рф/Новость"),
        ("https://xn--e1afmkfd.xn--p1ai/a", "https://xn--e1afmkfd.xn--p1ai/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_canonical_url_pairs(before: str, after: str) -> None:
    assert canonical_url(before, DROP) == after


def test_equivalent_urls_collapse() -> None:
    variants = [
        "https://example.com/news/42/?utm_source=a#top",
        "HTTPS://EXAMPLE.com:443/news/42?utm_campaign=b",
        "https://example.com/news/42",
    ]
    assert len({canonical_url(v, DROP) for v in variants}) == 1


@pytest.mark.parametrize("url", ["example.com/a", "/news/1", "", "https://"])
def test_url_without_scheme_or_host(url: str) -> None:
    with pytest.raises(ValueError, match="схемы или хоста"):
        canonical_url(url, DROP)


def test_custom_drop_patterns() -> None:
    assert canonical_url("https://e.com/a?ref_src=x&ref=y&id=1", ["ref*"]) == "https://e.com/a?id=1"
    assert canonical_url("https://e.com/a?utm_source=x", []) == "https://e.com/a?utm_source=x"


def test_html_paragraphs_and_entities() -> None:
    html_text = (
        "<div><h2>Заголовок</h2><p>Первый&nbsp;абзац с <b>жирным</b> и "
        "<a href='/x'>ссылкой</a>, точка.</p>"
        "<p>Второй\n   абзац &amp; &laquo;кавычки&raquo;<br>после переноса</p>"
        "<script>alert('x')</script><style>p{color:red}</style>"
        "<ul><li>Пункт один</li><li>Пункт два</li></ul></div>"
    )
    assert clean_html(html_text) == (
        "Заголовок\n\n"
        "Первый абзац с жирным и ссылкой, точка.\n\n"
        "Второй абзац & «кавычки» после переноса\n\n"
        "Пункт один\n\n"
        "Пункт два"
    )


def test_plain_text_paragraphs_kept() -> None:
    text = "Первая строка\nпродолжение\n\n\n  Второй абзац   с nbsp\n\nТретий"
    assert clean_html(text) == "Первая строка продолжение\n\nВторой абзац с nbsp\n\nТретий"


def test_double_escaped_entities() -> None:
    assert clean_html("Цена&amp;nbsp;100&amp;nbsp;₽ &amp;amp; скидка") == "Цена 100 ₽ & скидка"


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "<p></p>", "<div> &nbsp; </div>"])
def test_empty_inputs(text: str) -> None:
    assert clean_html(text) == ""


def test_text_with_angle_brackets() -> None:
    assert clean_html("Если a < b и b > c") == "Если a < b и b > c"
