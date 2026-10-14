# Шаг 084 из 088 · P048 · Этап 13 — Ежедневный отчёт в боте управления

**Задача трекера:** Ежедневный отчёт: данные
**Результат:** В publications сохраняется, кто опубликовал пост вне очереди (миграция 0003)
**Коммит:** `feat(db): кто инициировал публикацию`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/084_P048_kto-opublikoval-post.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Ежедневный отчёт должен разделять посты, вышедшие по расписанию, и посты, опубликованные админом вне очереди (шаг 076). Сейчас это видно только в логе.

1. **Модель** Publication (app/db/models.py): поле `initiated_by: Mapped[int | None]` (BigInteger, NULL — публикация по расписанию, иначе Telegram ID админа).
2. **Миграция** migrations/versions/0003_publication_initiator.py (revision "0003", down_revision "0002"): добавляет столбец, downgrade удаляет.
3. **publish_item** (app/publisher/stage_publish.py): keyword-only параметр `initiated_by: int | None = None`, который записывается в Publication — и в sent, и в failed. publish_due не передаёт его (None), publish_now передаёт initiator_id.

**Критерий готовности:** Публикация по расписанию сохраняет initiated_by = NULL, публикация из /queue — ID админа; миграция применяется и откатывается.

**Граничные случаи:** Неудачная ручная публикация тоже хранит initiated_by; старые строки после миграции имеют NULL; downgrade не теряет остальные данные.

**Тесты:** `tests/unit/test_stage_publish_now.py` — initiated_by в Publication для sent и failed; `tests/unit/test_stage_publish.py` — дописать одну проверку, что publish_due пишет None (остальное не меняй); `tests/integration/test_migrations.py` — столбец есть после upgrade и пропадает после downgrade до 0002.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async), Alembic, PostgreSQL 16.

**Файлы:** app/db/models.py, migrations/versions/0003_publication_initiator.py, app/publisher/stage_publish.py, tests/unit/test_stage_publish_now.py, tests/unit/test_stage_publish.py, tests/integration/test_migrations.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Миграции 0001 и 0002 не меняются. Новые зависимости, ключи конфигурации, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй.

**Формат ответа:** Полные файлы: app/db/models.py, migrations/versions/0003_publication_initiator.py, app/publisher/stage_publish.py, tests/unit/test_stage_publish_now.py, tests/unit/test_stage_publish.py, tests/integration/test_migrations.py. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): кто инициировал публикацию"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
