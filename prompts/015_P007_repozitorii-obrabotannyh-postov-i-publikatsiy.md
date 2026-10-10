# Шаг 015 из 070 · P007 · Этап 2 — База данных

**Задача трекера:** Репозитории доступа к данным
**Результат:** Репозитории ProcessedPost и Publication
**Коммит:** `feat(db): репозитории обработанных постов и публикаций`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/015_P007_repozitorii-obrabotannyh-postov-i-publikatsiy.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/db/repositories/posts.py: ProcessedPostRepository (add, get_by_news(news_id), update_summary(id, summary, edited_by), set_snooze(id, until), list_due_reviews(now), cost_since(dt) -> Decimal) и PublicationRepository (add, mark_sent(id, message_id, at), mark_failed(id, error), last_published_at(channel_id) -> datetime | None).

**Критерий готовности:** Методы работают на реальной БД; cost_since суммирует стоимость с момента dt.

**Граничные случаи:** cost_since без записей -> Decimal(`0`); last_published_at учитывает только status=sent; list_due_reviews не возвращает посты с snoozed_until в будущем.

**Тесты** (`tests/integration/test_repo_posts.py`): добавление и чтение, сумма стоимости по окну, last_published_at без отправок и с ошибками, snooze.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2.0 async, PostgreSQL 16.

**Файлы:** app/db/repositories/posts.py, tests/integration/test_repo_posts.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/db/repositories/posts.py, tests/integration/test_repo_posts.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/). Команды проверки: make db-up && make test-integration.

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): репозитории обработанных постов и публикаций"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
