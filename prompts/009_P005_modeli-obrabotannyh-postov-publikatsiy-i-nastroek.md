# Шаг 009 из 070 · P005 · Этап 2 — База данных

**Задача трекера:** Описать модели данных SQLAlchemy 2.0
**Результат:** Модели ProcessedPost, Publication, Keyword, Admin, Setting
**Коммит:** `feat(db): модели обработанных постов, публикаций и настроек`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/009_P005_modeli-obrabotannyh-postov-publikatsiy-i-nastroek.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Дополни app/db/models.py: ProcessedPost (id, news_item_id FK уникальный, ai_title, ai_summary, hashtags ARRAY(text), category, relevance_score, prompt_version, prompt_tokens, completion_tokens, cost, review_note nullable, snoozed_until nullable, edited_by nullable, created_at); Publication (id, processed_post_id FK, channel_id BIGINT, message_id nullable, published_at nullable, status, error nullable); Keyword (id, word, type include/exclude, уникальность (word, type)); Admin (telegram_id PK BIGINT, role admin/owner, created_at); Setting (key PK, value JSONB, updated_at). Перечисления KeywordType, AdminRole, PublicationStatus (sent, failed) — в app/domain/enums.py.

**Критерий готовности:** Все таблицы плана описаны; связи news_items -> processed_posts -> publications заданы.

**Граничные случаи:** Один news_item имеет не больше одного processed_post; cost хранится как Numeric, а не float.

**Тесты** (`tests/unit/test_models_posts.py`): состав таблиц, уникальные ограничения, тип cost, связи и каскады.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2.0, PostgreSQL ARRAY и JSONB.

**Файлы:** app/db/models.py, app/domain/enums.py, tests/unit/test_models_posts.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/db/models.py, app/domain/enums.py, tests/unit/test_models_posts.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): модели обработанных постов, публикаций и настроек"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
