# Шаг 104 из 113 · P057 · Этап 17 — Производительность БД

**Задача трекера:** Производительность БД
**Результат:** Индексы под выборки отчётов, бюджета и очереди
**Коммит:** `perf(db): индексы для отчётов, бюджета и очереди`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/104_P057_indeksy-dlya-otchetov-i-byudzheta.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Отчёты и проверка бюджета фильтруют по полям без индексов.

1. **Миграция 0007** (down_revision "0006"), индексы:
   - `ix_publications_channel_published` — publications (channel_id, published_at): last_published_at перед каждой публикацией, отчёты;
   - `ix_processed_posts_created_at` — processed_posts (created_at): воронка и отчёты;
   - `ix_news_items_created_at` — news_items (created_at): воронка и метрики по дате сбора;
   - `ix_news_items_source_status` — news_items (source_id, status): лимит источника (queued_by_source, queue_counts_by_source) и отчёт по источникам.
   Используй `CREATE INDEX CONCURRENTLY` через `op.create_index(..., postgresql_concurrently=True)` внутри `with op.get_context().autocommit_block():`, чтобы не блокировать запись на рабочей БД.
2. Те же индексы — в `__table_args__` моделей (app/db/models.py), чтобы схема из моделей совпадала с миграциями.
3. Индекс ai_requests.created_at уже создан на шаге 101 — не дублируй.

**Критерий готовности:** Миграция применяется на рабочей БД без блокировки записи; ключевые запросы используют индексы.

**Граничные случаи:** Повторный upgrade ничего не делает; downgrade удаляет только эти индексы; существующий индекс (status, created_at) не удаляется.

**Тесты:** `tests/integration/test_migrations.py` — индексы есть после upgrade, удаляются downgrade до 0006; `tests/integration/test_db_indexes.py` — EXPLAIN (при `SET enable_seqscan = off`) для last_published_at, cost_since, queue_counts_by_source и выборки воронки содержит Index Scan или Bitmap Index Scan по новым индексам.

**Контекст проекта:** Python 3.11+, Alembic, PostgreSQL 16.

**Файлы:** migrations/versions/0007_report_indexes.py, app/db/models.py, tests/integration/test_migrations.py, tests/integration/test_db_indexes.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). Смена статуса новости — только через set_status и app/domain/status.py. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Если меняется состав или порядок компонентов в app/main.py — обнови ожидания tests/unit/test_app_wiring.py; новая секция конфигурации должна быть описана в README.md (это проверяет tests/unit/test_readme.py).

**Формат ответа:** Полные файлы из списка «Файлы». Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "perf(db): индексы для отчётов, бюджета и очереди"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
