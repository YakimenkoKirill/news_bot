# Шаг 011 из 070 · P007 · Этап 2 — База данных

**Задача трекера:** Репозитории доступа к данным
**Результат:** SourceRepository
**Коммит:** `feat(db): репозиторий источников`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/011_P007_repozitoriy-istochnikov.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/db/repositories/sources.py: SourceRepository(session) с методами add, get, list_enabled(type=None), list_due(now) (включён и last_checked_at пуст или last_checked_at + interval_min <= now), mark_checked(id, at, fetch_state), mark_item_seen(id, at), register_error(id) -> новое значение error_count, reset_errors(id), set_enabled(id, enabled), delete(id).

**Критерий готовности:** Все методы работают на реальной PostgreSQL; list_due возвращает ровно источники, у которых наступило время проверки.

**Граничные случаи:** Источник без last_checked_at считается просроченным; выключенный источник не попадает в list_due; register_error атомарен при параллельных вызовах (UPDATE ... RETURNING).

**Тесты** (`tests/integration/test_repo_sources.py`): CRUD, list_due на границе интервала, выключенный источник, атомарный register_error.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2.0 async, PostgreSQL 16.

**Файлы:** app/db/repositories/sources.py, tests/integration/test_repo_sources.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/db/repositories/sources.py, tests/integration/test_repo_sources.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/). Команды проверки: make db-up && make test-integration.

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): репозиторий источников"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
