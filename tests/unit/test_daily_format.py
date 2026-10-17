import re
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.config import DailyReportConfig, MetricsConfig
from app.publisher.limits import utf16_len
from app.reports.daily import (
    DailyReport,
    Errors,
    Funnel,
    Headline,
    Publications,
    Quality,
    QueueState,
    SourceStats,
    Spending,
    TopPost,
)
from app.reports.daily_format import NEWS_FORMS, POST_FORMS, format_daily, plural
from app.reports.metrics import Period
from tests.config_helpers import base_section

MSK = ZoneInfo("Europe/Moscow")
PERIOD = Period.day(date(2026, 10, 14), MSK)
CFG = DailyReportConfig(**base_section("daily_report"))
METRICS = MetricsConfig(**base_section("metrics"))
BUDGET = Decimal("0.5")
LIMIT = 4096
TITLE = "14 октября"


def _report(**overrides: object) -> DailyReport:
    fields: dict[str, object] = {
        "period": PERIOD,
        "publications": Publications(),
        "sources": (),
        "funnel": Funnel(),
        "spending": Spending(),
        "quality": Quality(),
        "queue": QueueState(),
        "errors": Errors(),
        "previous": Headline(),
    }
    fields.update(overrides)
    return DailyReport(**fields)  # type: ignore[arg-type]


def _full() -> DailyReport:
    return _report(
        publications=Publications(
            total=12,
            scheduled=10,
            manual=2,
            edited=1,
            failed=1,
            by_hour={9: 3, 12: 5, 18: 4},
            top=(
                TopPost("Прорыв в ИИ", "Хабр", 0.91, 42, "https://t.me/c/1234567890/42"),
                TopPost("Новый процессор", "3DNews", 0.84, None, None),
            ),
        ),
        sources=(
            SourceStats(
                1,
                "Хабр",
                True,
                0,
                collected=40,
                published=8,
                duplicates=5,
                filtered=10,
                not_relevant=3,
                avg_score=0.72,
                cost=Decimal("0.02"),
            ),
            SourceStats(
                2,
                "3DNews",
                False,
                2,
                collected=20,
                published=4,
                duplicates=2,
                filtered=6,
                avg_score=0.55,
                cost=Decimal("0.01"),
            ),
        ),
        funnel=Funnel(
            collected=120,
            duplicates=18,
            duplicates_by_level={"url": 10, "simhash": 8},
            filtered=40,
            filtered_by_reason={"too_old": 30, "language": 10},
            processed=62,
            not_relevant=30,
            ai_failed=2,
            pending_review=2,
            rejected=1,
            in_queue=5,
            published=25,
        ),
        spending=Spending(
            cost=Decimal("0.21"),
            requests=62,
            failed=3,
            prompt_tokens=45200,
            cached_tokens=11300,
            completion_tokens=18300,
            budget_share=0.42,
            cost_per_post=Decimal("0.0175"),
            month_to_date=Decimal("3.4"),
        ),
        quality=Quality(
            delay_median_min=12.5,
            delay_p95_min=48.0,
            duplicate_share=0.01,
            avg_published_score=0.78,
            rejected_share=0.05,
        ),
        queue=QueueState(in_queue=5, pending_review=2, best_title="Лучший", best_score=0.88),
        errors=Errors(open_incidents=2, new_by_kind={"source_down": 1}, failed_news=3),
        previous=Headline(publications=9, collected=100, cost=Decimal("0.18")),
    )


def _format(report: DailyReport, limit: int = LIMIT, title: str = TITLE) -> list[str]:
    return format_daily(report, title, CFG, limit, budget=BUDGET, metrics=METRICS)


def _text(report: DailyReport, limit: int = LIMIT) -> str:
    return "\n\n".join(_format(report, limit))


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "пост"),
        (2, "поста"),
        (5, "постов"),
        (11, "постов"),
        (21, "пост"),
        (111, "постов"),
        (0, "постов"),
        (102, "поста"),
        (114, "постов"),
    ],
)
def test_plural(count: int, expected: str) -> None:
    assert plural(count, POST_FORMS) == expected


def test_plural_news_forms() -> None:
    assert [plural(n, NEWS_FORMS) for n in (1, 3, 5)] == ["новость", "новости", "новостей"]


def test_header_and_headline() -> None:
    text = _text(_full())
    assert text.startswith("📊 <b>Отчёт за 14 октября</b>")
    assert "Опубликовано 12 постов ▲ +3" in text
    assert "собрано 120 ▲ +20" in text
    assert "потрачено $0.21 ▲ +$0.03" in text


def test_paused_marked() -> None:
    report = _report(queue=QueueState(paused=True))
    assert "⏸ Пайплайн на паузе" in _text(report)


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [(5, 3, "▲ +2"), (3, 5, "▼ −2"), (4, 4, "= 0"), (0, 0, "= 0")],
)
def test_delta_arrows(current: int, previous: int, expected: str) -> None:
    report = _report(
        publications=Publications(total=current), previous=Headline(publications=previous)
    )
    assert f"Опубликовано {current} {plural(current, POST_FORMS)} {expected}" in _text(report)


def test_publications_block() -> None:
    text = _text(_full())
    assert "по расписанию 10 · вне очереди 2 · отредактировано 1 · неудачных 1" in text
    assert "Самый активный час: 12:00 — 5 постов" in text
    assert '<a href="https://t.me/c/1234567890/42">★0.91 Прорыв в ИИ — Хабр</a>' in text
    assert "• ★0.84 Новый процессор — 3DNews" in text


def test_sources_table() -> None:
    text = _text(_full())
    assert "<pre>" in text and "</pre>" in text
    assert "источник          собр  канал  дубл  отсев  оценка" in text
    assert "Хабр" in text and "3DNews⚠️" in text  # ошибки важнее отметки «выключен»
    assert "0.72" in text


def test_sources_marks_errors_and_disabled() -> None:
    report = _report(
        sources=(
            SourceStats(1, "Сломан", True, 3, collected=1),
            SourceStats(2, "Выключен", False, 0, collected=1),
            SourceStats(3, "Оба", False, 2, collected=1),
            SourceStats(4, "Обычный", True, 0, collected=1),
        )
    )
    text = _text(report)
    assert "Сломан⚠️" in text
    assert "Выключен⏸" in text
    assert "Оба⚠️" in text, "ошибки показываем важнее, чем «выключен»"
    assert "Обычный " in text


def test_sources_truncated_to_top() -> None:
    sources = tuple(
        SourceStats(i, f"Источник {i}", True, 0, collected=100 - i, published=50 - i)
        for i in range(1, 51)
    )
    text = _text(_report(sources=sources))
    assert "прочие (40)" in text
    assert text.count("Источник ") == CFG.top_sources


def test_long_source_name_cut() -> None:
    long_name = "Очень длинное имя источника без конца"
    text = _text(_report(sources=(SourceStats(1, long_name, True, 0, collected=1),)))
    assert long_name not in text
    assert "…" in text


def test_html_escaped_everywhere() -> None:
    report = _report(
        publications=Publications(
            total=1, top=(TopPost("Ссылка <b>&</b>", "A & B <News>", 0.5, 1, "https://t.me/c/1/1"),)
        ),
        sources=(SourceStats(1, "A & B <x>", True, 0, collected=1),),
        queue=QueueState(in_queue=1, best_title="Пост <b>&</b>"),
    )
    text = _text(report)
    assert "&amp;" in text
    assert "<b>&</b>" not in text
    assert "&lt;News&gt;" in text
    assert "&lt;x&gt;" in text


def test_funnel_block() -> None:
    text = _text(_full())
    assert (
        "Собрано 120 → дубли 18 → фильтры 40 → модель 62 → не по теме 30 → "
        "на модерации 2 → в очереди 5 → опубликовано 25" in text
    )
    assert "Фильтры: language 10, too_old 30" in text
    assert "Дубли: simhash 8, url 10" in text
    assert "Ошибки модели: 2" in text


def test_spending_block() -> None:
    text = _text(_full())
    assert "$0.21 из $0.5 (42%)" in text
    assert "Запросов: 62 · токены 45 200 / 18 300" in text
    assert "Стоимость поста: $0.0175" in text
    assert "С начала месяца: $3.4" in text
    assert "⚠️" not in text.split("<b>Качество</b>")[0].split("<b>Модель</b>")[1]


def test_cache_and_failed_requests_in_spending_block() -> None:
    text = _text(_full())
    assert "Из кэша 25% входных токенов" in text
    assert "Неудачных запросов: 3" in text


def test_cache_line_hidden_without_input_tokens() -> None:
    model_block = _text(_report(spending=Spending(requests=0))).split("<b>Модель</b>")[1]
    assert "Из кэша" not in model_block
    assert "Неудачных запросов" not in model_block


def test_cache_line_shows_zero_share() -> None:
    spending = Spending(prompt_tokens=1000, cached_tokens=0, requests=1)
    assert "Из кэша 0% входных токенов" in _text(_report(spending=spending))


def test_budget_warning_above_threshold() -> None:
    report = _report(spending=Spending(cost=Decimal("0.45"), budget_share=0.9))
    model_block = _text(report).split("<b>Модель</b>")[1]
    assert "⚠️" in model_block


def test_budget_warning_at_threshold() -> None:
    report = _report(spending=Spending(budget_share=CFG.budget_warn_share))
    assert "⚠️" in _text(report).split("<b>Модель</b>")[1]


def test_quality_marks_targets() -> None:
    text = _text(_full())
    assert "медиана 12.5 мин ✅" in text
    assert "Дублей в канале: 1.0% ✅" in text
    assert "Отклонено модератором: 5.0% ✅" in text


def test_quality_failed_targets() -> None:
    report = _report(
        quality=Quality(
            delay_median_min=METRICS.max_delay_min + 10,
            duplicate_share=METRICS.max_duplicate_share + 0.5,
            rejected_share=METRICS.max_rejected_share + 0.5,
        )
    )
    text = _text(report)
    assert "❌" in text
    assert text.count("❌") == 3


def test_queue_and_errors_blocks() -> None:
    text = _text(_full())
    assert "В очереди: 5 (лучший: ★0.88 «Лучший»)" in text
    assert "На модерации: 2" in text
    assert "Открыто 2 · новых за период 1" in text
    assert "По видам: source_down 1" in text
    assert "Новостей с ошибкой: 3 новости" in text
    assert "Подробнее: /errors" in text


def test_empty_day_is_short() -> None:
    messages = _format(_report())
    assert len(messages) == 1
    text = messages[0]
    assert "Опубликовано 0 постов = 0" in text
    assert "<b>Модель</b>" in text
    assert "Ошибок нет ✅" in text
    assert "<b>Публикации</b>" not in text
    assert "<b>Источники</b>" not in text
    assert "<b>Воронка</b>" not in text
    assert "<b>Качество</b>" not in text
    assert "<b>Сейчас</b>" not in text


def test_messages_fit_limit_and_tags_balanced() -> None:
    sources = tuple(
        SourceStats(i, f"Источник {i}", True, 0, collected=i, published=i, avg_score=0.5)
        for i in range(1, 11)
    )
    report = _full()
    report = _report(
        publications=report.publications,
        sources=sources,
        funnel=report.funnel,
        spending=report.spending,
        quality=report.quality,
        queue=report.queue,
        errors=report.errors,
        previous=report.previous,
    )
    messages = _format(report, limit=300)
    assert len(messages) > 1
    for message in messages:
        assert utf16_len(message) <= 300
        assert message.count("<pre>") == message.count("</pre>")
        assert message.count("<b>") == message.count("</b>")
        assert message.count("<a ") == message.count("</a>")


def test_split_table_keeps_all_rows() -> None:
    sources = tuple(SourceStats(i, f"Источник {i}", True, 0, collected=i) for i in range(1, 11))
    messages = _format(_report(sources=sources), limit=220)
    joined = "\n".join(messages)
    for index in range(1, 11):
        assert f"Источник {index}" in joined
    for message in messages:
        assert message.count("<pre>") == message.count("</pre>")


def test_no_stray_tags_in_any_message() -> None:
    for message in _format(_full(), limit=250):
        assert not re.search(r"<pre>(?:(?!</pre>).)*$", message, re.S)


def test_funnel_shows_queue_limited() -> None:
    report = _report(funnel=Funnel(collected=10, processed=8, queue_limited=3))
    assert "вытеснено лимитом источника 3 →" in _text(report)


def test_funnel_hides_zero_queue_limited() -> None:
    report = _report(funnel=Funnel(collected=10, processed=8))
    assert "вытеснено лимитом" not in _text(report)


def test_displaced_line_under_sources_table() -> None:
    report = _report(
        sources=(
            SourceStats(1, "Bell", True, 0, collected=10, published=2, queue_limited=3),
            SourceStats(2, "Хабр", True, 0, collected=5, published=1, queue_limited=1),
            SourceStats(3, "РБК", True, 0, collected=4, published=1),
        )
    )
    text = _text(report)
    assert "Вытеснено лимитом: Bell 3, Хабр 1" in text
    assert "РБК" in text, "источник без вытеснений остаётся в таблице"


def test_no_displaced_line_without_limits() -> None:
    report = _report(sources=(SourceStats(1, "Bell", True, 0, collected=1),))
    assert "Вытеснено лимитом" not in _text(report)
