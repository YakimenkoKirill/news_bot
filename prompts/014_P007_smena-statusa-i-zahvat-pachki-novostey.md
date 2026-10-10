# Шаг 014 из 070 · P007 · Этап 2 — База данных

**Задача трекера:** Репозитории доступа к данным
**Результат:** NewsRepository: смена статуса и захват пачки
**Коммит:** `feat(db): смена статуса и захват пачки новостей`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/014_P007_smena-statusa-i-zahvat-pachki-novostey.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Дополни NewsRepository: set_status(id, expected, new, reason=None) — проверяет переход через ensure_transition и выполняет UPDATE ... WHERE id = :id AND status = :expected, при 0 строк бросает StaleStatusError; claim_batch(status, limit) -> list[NewsItem] через SELECT ... FOR UPDATE SKIP LOCKED ORDER BY created_at (блокировка держится до конца транзакции вызывающего); update_fields(id, **fields) для нормализованных полей; count_by_status() -> dict[NewsStatus, int].

**Критерий готовности:** Две параллельные транзакции claim_batch получают непересекающиеся наборы; set_status отклоняет недопустимые и устаревшие переходы.

**Граничные случаи:** Пустая очередь -> пустой список; limit <= 0 -> ValueError; count_by_status возвращает 0 для статусов без записей.

**Тесты** (`tests/integration/test_repo_news_status.py`): параллельный claim двумя сессиями, недопустимый переход, устаревший expected, count_by_status со всеми ключами.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2.0 async, PostgreSQL 16.

**Файлы:** app/db/repositories/news.py, tests/integration/test_repo_news_status.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/db/repositories/news.py, tests/integration/test_repo_news_status.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/). Команды проверки: make db-up && make test-integration.

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): смена статуса и захват пачки новостей"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
