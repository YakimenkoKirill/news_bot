# Шаг 008 из 070 · P005 · Этап 2 — База данных

**Задача трекера:** Описать модели данных SQLAlchemy 2.0
**Результат:** app/domain/enums.py + модели Source и NewsItem
**Коммит:** `feat(db): модели Source и NewsItem и перечисления`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/008_P005_modeli-source-i-newsitem-i-perechisleniya.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Создай app/domain/enums.py: SourceType (rss, web, tg), ModerationMode (auto, review), NewsStatus (new, duplicate, filtered, normalized, processed, pending_review, approved, rejected, scheduled, published, failed). В app/db/models.py опиши Source (id, type, url уникальный, name, interval_min, enabled, moderation_mode, selectors JSONB nullable, fetch_state JSONB по умолчанию {}, last_checked_at, last_item_at, error_count, created_at) и NewsItem (id, source_id FK, external_id, url, canonical_url, title, raw_text, clean_text, image_url, language, published_at, content_hash, simhash BIGINT, status, reject_reason, cluster_id FK на news_items nullable, raw_meta JSONB, created_at, updated_at). Все даты — timezone-aware (TIMESTAMP WITH TIME ZONE). Enum в БД хранится строкой (native_enum=False).

**Критерий готовности:** Модели описаны по таблице плана; перечисление NewsStatus совпадает с жизненным циклом новости.

**Граничные случаи:** updated_at обновляется при изменении; удаление источника каскадно удаляет его новости; simhash помещается в BIGINT со знаком.

**Тесты** (`tests/unit/test_models_news.py`): состав таблиц и колонок в metadata, типы дат с часовым поясом, значения NewsStatus, ondelete=CASCADE для source_id.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2.0 (Mapped, mapped_column).

**Файлы:** app/domain/__init__.py, app/domain/enums.py, app/db/models.py, tests/unit/test_models_news.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/domain/__init__.py, app/domain/enums.py, app/db/models.py, tests/unit/test_models_news.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): модели Source и NewsItem и перечисления"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
