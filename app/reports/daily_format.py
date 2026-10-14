"""Текст ежедневного отчёта в HTML для Telegram."""

from __future__ import annotations

import html
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from app.config import DailyReportConfig, MetricsConfig
from app.publisher.limits import utf16_len
from app.reports.daily import DailyReport, SourceStats
from app.reports.metrics import MetricCheck, RunReport, compare_with_targets

PARAGRAPH = "\n\n"
NO_DATA = "нет данных"
MONEY = Decimal("0.0001")
PRE_OPEN = "<pre>"
PRE_CLOSE = "</pre>"
POST_FORMS = ("пост", "поста", "постов")
NEWS_FORMS = ("новость", "новости", "новостей")
TABLE_HEADER = "источник          собр  канал  дубл  отсев  оценка"
PERCENT = 100
TEENS = range(11, 15)
LAST_DIGIT_ONE = 1
LAST_DIGIT_FEW = range(2, 5)


def plural(count: int, forms: Sequence[str]) -> str:
    """Склонение существительного: formы — для 1, 2 и 5."""
    if count % PERCENT in TEENS:
        return forms[2]
    last = count % 10
    if last == LAST_DIGIT_ONE:
        return forms[0]
    if last in LAST_DIGIT_FEW:
        return forms[1]
    return forms[2]


def _money(value: Decimal) -> str:
    return f"${value.quantize(MONEY, rounding=ROUND_HALF_UP).normalize():f}"


def _thousands(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _delta(current: int, previous: int) -> str:
    """Изменение к прошлому периоду: ▲ +3, ▼ −2 или = 0."""
    diff = current - previous
    if diff > 0:
        return f"▲ +{diff}"
    if diff < 0:
        return f"▼ −{abs(diff)}"
    return "= 0"


def _money_delta(current: Decimal, previous: Decimal) -> str:
    diff = current - previous
    if diff > 0:
        return f"▲ +{_money(diff)}"
    if diff < 0:
        return f"▼ −{_money(abs(diff))}"
    return "= $0"


def _minutes(value: float | None) -> str:
    return NO_DATA if value is None else f"{value:.1f} мин"


def _share(value: float | None) -> str:
    return NO_DATA if value is None else f"{value * PERCENT:.1f}%"


def _score(value: float | None) -> str:
    return NO_DATA if value is None else f"★{value:.2f}"


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _headline_block(report: DailyReport) -> str:
    spending, previous = report.spending, report.previous
    published = report.publications.total
    collected = report.funnel.collected
    return (
        f"Опубликовано {published} {plural(published, POST_FORMS)} "
        f"{_delta(published, previous.publications)} · "
        f"собрано {collected} {_delta(collected, previous.collected)} · "
        f"потрачено {_money(spending.cost)} {_money_delta(spending.cost, previous.cost)}"
    )


def _publications_block(report: DailyReport) -> str | None:
    pubs = report.publications
    if not pubs.total and not pubs.failed:
        return None
    lines = [
        "<b>Публикации</b>",
        f"по расписанию {pubs.scheduled} · вне очереди {pubs.manual} · "
        f"отредактировано {pubs.edited} · неудачных {pubs.failed}",
    ]
    if pubs.by_hour:
        hour, count = max(pubs.by_hour.items(), key=lambda item: (item[1], -item[0]))
        lines.append(f"Самый активный час: {hour:02d}:00 — {count} {plural(count, POST_FORMS)}")
    if pubs.top:
        lines.append("Лучшие посты:")
        for post in pubs.top:
            title = _escape(post.title)
            label = f"{_score(post.score)} {title} — {_escape(post.source)}"
            lines.append(f'• <a href="{post.url}">{label}</a>' if post.url else f"• {label}")
    return "\n".join(lines)


def _source_row(item: SourceStats, cfg: DailyReportConfig) -> str:
    mark = "⚠️" if item.error_count else ("⏸" if not item.enabled else "")
    name = _cut(item.name, cfg.source_name_chars)
    score = f"{item.avg_score:.2f}" if item.avg_score is not None else "—"
    return (
        f"{_escape(name + mark):<20}"
        f"{item.collected:>4}{item.published:>7}{item.duplicates:>6}"
        f"{item.filtered + item.not_relevant:>7}{score:>8}"
    )


def _sources_block(report: DailyReport, cfg: DailyReportConfig) -> str | None:
    if not report.sources:
        return None
    shown = report.sources[: cfg.top_sources]
    rest = report.sources[cfg.top_sources :]
    rows = [TABLE_HEADER, *(_source_row(item, cfg) for item in shown)]
    if rest:
        collected = sum(item.collected for item in rest)
        published = sum(item.published for item in rest)
        rows.append(f"{f'прочие ({len(rest)})':<20}{collected:>4}{published:>7}")
    return "<b>Источники</b>\n" + PRE_OPEN + "\n".join(rows) + PRE_CLOSE


def _funnel_block(report: DailyReport) -> str | None:
    funnel = report.funnel
    if not funnel.collected:
        return None
    lines = [
        "<b>Воронка</b>",
        f"Собрано {funnel.collected} → дубли {funnel.duplicates} → "
        f"фильтры {funnel.filtered} → модель {funnel.processed} → "
        f"не по теме {funnel.not_relevant} → на модерации {funnel.pending_review} → "
        f"в очереди {funnel.in_queue} → опубликовано {funnel.published}",
    ]
    if funnel.filtered_by_reason:
        details = ", ".join(f"{k} {v}" for k, v in sorted(funnel.filtered_by_reason.items()))
        lines.append(f"Фильтры: {details}")
    if funnel.duplicates_by_level:
        details = ", ".join(f"{k} {v}" for k, v in sorted(funnel.duplicates_by_level.items()))
        lines.append(f"Дубли: {details}")
    if funnel.ai_failed:
        lines.append(f"Ошибки модели: {funnel.ai_failed}")
    return "\n".join(lines)


def _spending_block(report: DailyReport, cfg: DailyReportConfig, budget: Decimal) -> str:
    spending = report.spending
    share = spending.budget_share
    warn = " ⚠️" if share is not None and share >= cfg.budget_warn_share else ""
    percent = f" ({share * PERCENT:.0f}%)" if share is not None else ""
    lines = [
        "<b>Модель</b>",
        f"{_money(spending.cost)} из {_money(budget)}{percent}{warn}",
        f"Запросов: {spending.requests} · токены "
        f"{_thousands(spending.prompt_tokens)} / {_thousands(spending.completion_tokens)}",
    ]
    if spending.cost_per_post is not None:
        lines.append(f"Стоимость поста: {_money(spending.cost_per_post)}")
    lines.append(f"С начала месяца: {_money(spending.month_to_date)}")
    return "\n".join(lines)


def _check_mark(check: MetricCheck) -> str:
    return "" if check.ok is None else (" ✅" if check.ok else " ❌")


def _quality_block(report: DailyReport, metrics: MetricsConfig) -> str | None:
    quality = report.quality
    values = (
        quality.delay_median_min,
        quality.delay_p95_min,
        quality.avg_published_score,
        quality.duplicate_share,
        quality.rejected_share,
    )
    if all(value is None for value in values):
        return None
    base = RunReport(
        delay_median_min=quality.delay_median_min,
        rejected_share=quality.rejected_share,
        duplicate_share=quality.duplicate_share,
    )
    delay, rejected, duplicates = compare_with_targets(base, metrics)
    return "\n".join(
        [
            "<b>Качество</b>",
            f"Задержка: медиана {_minutes(quality.delay_median_min)}{_check_mark(delay)} · "
            f"p95 {_minutes(quality.delay_p95_min)}",
            f"Средняя оценка постов: {_score(quality.avg_published_score)}",
            f"Дублей в канале: {_share(quality.duplicate_share)}{_check_mark(duplicates)}",
            f"Отклонено модератором: {_share(quality.rejected_share)}{_check_mark(rejected)}",
        ]
    )


def _queue_block(report: DailyReport) -> str | None:
    queue = report.queue
    if not queue.in_queue and not queue.pending_review:
        return None
    lines = ["<b>Сейчас</b>"]
    best = ""
    if queue.best_title is not None:
        best = f" (лучший: {_score(queue.best_score)} «{_escape(queue.best_title)}»)"
    lines.append(f"В очереди: {queue.in_queue}{best}")
    lines.append(f"На модерации: {queue.pending_review}")
    return "\n".join(lines)


def _errors_block(report: DailyReport) -> str:
    errors = report.errors
    if not errors.open_incidents and not errors.new_by_kind and not errors.failed_news:
        return "<b>Ошибки</b>\nОшибок нет ✅"
    new_total = sum(errors.new_by_kind.values())
    lines = ["<b>Ошибки</b>", f"Открыто {errors.open_incidents} · новых за период {new_total}"]
    if errors.new_by_kind:
        details = ", ".join(f"{k} {v}" for k, v in sorted(errors.new_by_kind.items()))
        lines.append(f"По видам: {details}")
    if errors.failed_news:
        failed = errors.failed_news
        lines.append(f"Новостей с ошибкой: {failed} {plural(failed, NEWS_FORMS)}")
    if errors.open_incidents:
        lines.append("Подробнее: /errors")
    return "\n".join(lines)


def _split_block(block: str, limit: int) -> list[str]:
    """Режет длинный блок по строкам, заново открывая <pre> в каждом куске."""
    lines = block.replace(PRE_CLOSE, "").split("\n")
    inside = False
    chunks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        text = "\n".join(current)
        chunks.append(text + PRE_CLOSE if PRE_OPEN in text else text)
        current.clear()

    for line in lines:
        candidate = [*current, line]
        extra = len(PRE_CLOSE) if inside or PRE_OPEN in line else 0
        if current and utf16_len("\n".join(candidate)) + extra > limit:
            flush()
            if inside:
                current.append(PRE_OPEN)
        current.append(line)
        if PRE_OPEN in line:
            inside = True
    flush()
    return chunks


def _pack(blocks: Sequence[str], limit: int) -> list[str]:
    """Собирает блоки в сообщения не длиннее limit, разрывая только между блоками."""
    messages: list[str] = []
    current: list[str] = []
    for block in blocks:
        candidate = [*current, block]
        if current and utf16_len(PARAGRAPH.join(candidate)) > limit:
            messages.append(PARAGRAPH.join(current))
            current = []
        if utf16_len(block) > limit:
            if current:
                messages.append(PARAGRAPH.join(current))
                current = []
            messages.extend(_split_block(block, limit))
            continue
        current.append(block)
    if current:
        messages.append(PARAGRAPH.join(current))
    return messages


def format_daily(
    report: DailyReport,
    title: str,
    cfg: DailyReportConfig,
    text_limit: int,
    *,
    budget: Decimal,
    metrics: MetricsConfig,
) -> list[str]:
    """Собирает отчёт в HTML-сообщения для Telegram.

    Блоки разделяются пустой строкой; сообщение режется только между блоками,
    а слишком длинная таблица источников — по строкам, с повторным открытием <pre>.

    Args:
        report: Данные отчёта.
        title: Период в заголовке («14 октября», «вчера»).
        cfg: Секция daily_report.
        text_limit: Лимит одного сообщения (publisher.text_limit).
        budget: Дневной бюджет на модель (ai.daily_budget).
        metrics: Цели MVP для отметок ✅/❌.

    Returns:
        Одно или несколько сообщений, каждое не длиннее text_limit.
    """
    head = [f"📊 <b>Отчёт за {_escape(title)}</b>"]
    if report.queue.paused:
        head.append("⏸ Пайплайн на паузе")
    head.append(_headline_block(report))
    blocks: list[str | None] = [
        "\n".join(head),
        _publications_block(report),
        _sources_block(report, cfg),
        _funnel_block(report),
        _spending_block(report, cfg, budget),
        _quality_block(report, metrics),
        _queue_block(report),
        _errors_block(report),
    ]
    return _pack([block for block in blocks if block], text_limit)
