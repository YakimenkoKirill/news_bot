# Шаг 010 из 070 · P006 · Этап 2 — База данных

**Задача трекера:** Первая миграция Alembic с индексами и ограничениями
**Результат:** Alembic (async) + миграция 0001 со всеми таблицами и индексами
**Коммит:** `feat(db): alembic и первая миграция с индексами`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/010_P006_alembic-i-pervaya-migratsiya-s-indeksami.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Инициализируй Alembic в каталоге migrations/ с асинхронным env.py, который берёт database_url из get_settings() и target_metadata из app.db.base.Base. Напиши миграцию 0001: все таблицы из app/db/models.py, уникальный индекс news_items(source_id, external_id), индексы news_items(canonical_url), news_items(content_hash), news_items(status, created_at), news_items(cluster_id). Добавь цели Makefile migrate (alembic upgrade head) и migration (alembic revision --autogenerate -m).

**Критерий готовности:** alembic upgrade head на чистой БД создаёт схему; downgrade base её удаляет; автогенерация после upgrade не находит различий с моделями.

**Граничные случаи:** Повторный upgrade head ничего не делает; миграция не зависит от данных в .env кроме DATABASE_URL.

**Тесты** (`tests/integration/test_migrations.py`): upgrade/downgrade на чистой БД, наличие индексов через inspector, пустой diff compare_metadata (маркер integration, БД из TEST_DATABASE_URL).

**Контекст проекта:** Python 3.11+, Alembic, SQLAlchemy 2.0 async, PostgreSQL 16.

**Файлы:** alembic.ini, migrations/env.py, migrations/script.py.mako, migrations/versions/0001_initial.py, Makefile, tests/integration/conftest.py, tests/integration/test_migrations.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: alembic.ini, migrations/env.py, migrations/script.py.mako, migrations/versions/0001_initial.py, Makefile, tests/integration/conftest.py, tests/integration/test_migrations.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/). Команды проверки: make db-up && make test-integration.

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): alembic и первая миграция с индексами"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
