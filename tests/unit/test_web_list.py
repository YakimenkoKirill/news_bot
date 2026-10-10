from pathlib import Path

import pytest
from pydantic import ValidationError

from app.collectors.web_list import ArticleLink, WebSelectors, extract_links

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "web"


def _html(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


SITE_A = WebSelectors(
    list_item="article.news-card", link="a.news-card__link", title=".news-card__title"
)
SITE_B = WebSelectors(list_item="#feed a.item")


def test_site_a_links_with_titles() -> None:
    links = extract_links(_html("list_site_a.html"), "https://site-a.example/news/", SITE_A)
    assert links == [
        ArticleLink(
            url="https://site-a.example/news/2026/10/cpu",
            title="Компания представила новый процессор",
        ),
        ArticleLink(
            url="https://site-a.example/news/news/2026/10/speech",
            title="Учёные обучили модель распознавать речь",
        ),
        ArticleLink(url="https://partner.example.net/story/42", title="Новость с другого домена"),
    ]


def test_site_b_list_item_is_link_itself() -> None:
    links = extract_links(_html("list_site_b.html"), "https://site-b.example/", SITE_B)
    assert links == [
        ArticleLink(
            url="https://site-b.example/posts/101", title="Запуск ракеты перенесли на неделю"
        ),
        ArticleLink(
            url="https://site-b.example/posts/102", title="Город открыл новую станцию метро"
        ),
        ArticleLink(
            url="https://site-b.example/posts/103?utm_source=list", title="Курс валют на сегодня"
        ),
    ]


def test_relative_links_resolved_against_base() -> None:
    html = '<div class="i"><a href="../x/1">Один</a></div><div class="i"><a href="2">Два</a></div>'
    links = extract_links(html, "https://example.com/a/b/", WebSelectors(list_item="div.i"))
    assert [link.url for link in links] == [
        "https://example.com/a/x/1",
        "https://example.com/a/b/2",
    ]


def test_skips_other_schemes_and_items_without_link() -> None:
    html = """
    <p class="i"><a href="mailto:a@b.c">m</a></p>
    <p class="i"><a href="javascript:alert(1)">j</a></p>
    <p class="i"><a href="tel:+70000000000">t</a></p>
    <p class="i"><a>без href</a></p>
    <p class="i">без ссылки</p>
    <p class="i"><a href="https://ok.example/1">ok</a></p>
    """
    links = extract_links(html, "https://example.com/", WebSelectors(list_item="p.i"))
    assert links == [ArticleLink(url="https://ok.example/1", title="ok")]


def test_duplicates_removed_keeping_first() -> None:
    html = """
    <div class="i"><a href="/a">Первая</a></div>
    <div class="i"><a href="/a#top">Повтор с якорем</a></div>
    <div class="i"><a href="https://example.com/a">Повтор абсолютный</a></div>
    """
    links = extract_links(html, "https://example.com/", WebSelectors(list_item="div.i"))
    assert links == [ArticleLink(url="https://example.com/a", title="Первая")]


def test_empty_page_returns_empty_list() -> None:
    assert extract_links("", "https://example.com/", SITE_B) == []


@pytest.mark.parametrize(
    "fields",
    [
        {"list_item": "div[["},
        {"list_item": "div.i", "link": "a:::bad"},
        {"list_item": "div.i", "title": ">>"},
        {"list_item": "   "},
        {"list_item": "div.i", "unknown": "x"},
    ],
)
def test_invalid_selectors_rejected(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        WebSelectors(**fields)


def test_selectors_from_jsonb_dict() -> None:
    selectors = WebSelectors.model_validate({"list_item": "li", "link": "a", "title": "h3"})
    assert selectors.model_dump() == {"list_item": "li", "link": "a", "title": "h3"}
