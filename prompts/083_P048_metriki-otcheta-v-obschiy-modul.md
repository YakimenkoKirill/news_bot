# Шаг 083 из 088 · P048 · Этап 13 — Ежедневный отчёт в боте управления

**Задача трекера:** Ежедневный отчёт: данные
**Результат:** Подсчёт метрик вынесен из scripts/run_report.py в пакет app/reports/
**Коммит:** `refactor(reports): метрики отчёта в пакете app/reports`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/083_P048_metriki-otcheta-v-obschiy-modul.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Метрики тестового прогона (публикации, задержка, дубли, расход, ошибки источников) сейчас считаются в scripts/run_report.py, и приложение не может их импортировать. Ежедневный отчёт в боте (шаги 085–088) должен считать то же самое тем же кодом, поэтому вынеси подсчёт в пакет приложения без изменения поведения.

1. Создай пакет app/reports/ (`__init__.py`) и модуль app/reports/metrics.py. Перенеси туда без изменений логики: `Period` (с `from_dates`), `RunReport`, `MetricCheck`, `percentile`, `collect_report`, `compare_with_targets` и нужные константы (P95, DUPLICATE_PREFIX, REVIEWED_STATUSES и др.).
2. Добавь в `Period` метод `day(day: date, tz: ZoneInfo) -> Period` — сутки [00:00, 24:00) в часовом поясе tz (через from_dates(day, day, tz)).
3. scripts/run_report.py оставляет только CLI и форматирование (`format_report`, `_run`, `main`) и импортирует остальное из app.reports.metrics. Чтобы существующие импорты в тестах не сломались, он реэкспортирует перенесённые имена (`__all__`).
4. Обнови структуру каталогов в .claude/CLAUDE.md (раздел «Структура каталогов», не «Автопилот»): строка `reports/ метрики и ежедневный отчёт`.

**Критерий готовности:** `scripts/run_report.py --from … --to …` печатает тот же текст, что и раньше; tests/integration/test_run_report.py проходит без изменений; app.reports.metrics импортируется без scripts.

**Граничные случаи:** Period.day через переход на летнее/зимнее время (зона с DST, например Europe/Berlin) — 23 или 25 часов, а не ровно 24; пустой период — метрики None, как раньше.

**Тесты:** `tests/unit/test_report_metrics.py` — percentile (один элемент, интерполяция), compare_with_targets (нет данных, в пределах, сверх цели), Period.day в Europe/Moscow и в зоне с DST. tests/integration/test_run_report.py не меняй.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async). Это чистый рефакторинг перед шагом 085.

**Файлы:** app/reports/__init__.py, app/reports/metrics.py, scripts/run_report.py, .claude/CLAUDE.md, tests/unit/test_report_metrics.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Раздел «Автопилот» в CLAUDE.md не меняй. Поведение и вывод скрипта не меняй. Новые зависимости, ключи конфигурации, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не меняй и не ослабляй.

**Формат ответа:** Полные файлы: app/reports/__init__.py, app/reports/metrics.py, scripts/run_report.py, tests/unit/test_report_metrics.py; для .claude/CLAUDE.md — изменённый блок структуры. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "refactor(reports): метрики отчёта в пакете app/reports"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
