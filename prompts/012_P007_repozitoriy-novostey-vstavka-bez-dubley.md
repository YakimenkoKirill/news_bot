# Шаг 012 из 070 · P007 · Этап 2 — База данных

**Задача трекера:** Репозитории доступа к данным
**Результат:** NewsRepository: вставка без дублей и поиск
**Коммит:** `feat(db): репозиторий новостей, вставка без дублей`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/012_P007_repozitoriy-novostey-vstavka-bez-dubley.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/db/repositories/news.py: NewsRepository(session) с методами add_if_new(raw) -> NewsItem | None (INSERT ... ON CONFLICT (source_id, external_id) DO NOTHING RETURNING), exists_url(canonical_url) -> bool, find_by_hash(content_hash, since) -> NewsItem | None, recent_simhashes(since) -> list[tuple[int, int, int | None]] (id, simhash, cluster_id), max_tg_message_id(source_id) -> int | None (максимум raw_meta->>'msg_id').

**Критерий готовности:** Повторная вставка той же новости возвращает None и не создаёт запись; поиск учитывает окно since.

**Граничные случаи:** Новости старше since не возвращаются; пустая таблица -> None/пустой список; msg_id сравнивается как число, а не строка.

**Тесты** (`tests/integration/test_repo_news_add.py`): вставка и повтор, exists_url, find_by_hash в окне и вне окна, recent_simhashes, max_tg_message_id с числами разной длины.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2.0 async (postgresql.insert), PostgreSQL 16.

**Файлы:** app/db/repositories/news.py, tests/integration/test_repo_news_add.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/db/repositories/news.py, tests/integration/test_repo_news_add.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/). Команды проверки: make db-up && make test-integration.

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): репозиторий новостей, вставка без дублей"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
