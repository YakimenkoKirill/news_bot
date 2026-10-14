# Шаг 078 из 082 · P045 · Этап 12 — Журнал ошибок в боте управления

**Задача трекера:** Журнал ошибок: хранение событий
**Результат:** Таблица incidents (миграция 0002), модель Incident и IncidentRepository
**Коммит:** `feat(db): журнал ошибок — таблица incidents и репозиторий`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/078_P045_zhurnal-oshibok-tablitsa-i-repozitoriy.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Сейчас алерт только отправляется админам и нигде не сохраняется, а повторы, подавленные throttle_min, теряются совсем. Сохраняй каждое событие в БД, чтобы ошибки можно было смотреть командой /errors (шаги 081–082).

**Одна запись — один инцидент, а не одно событие.** Повторы с тем же key, пока инцидент не закрыт, увеличивают счётчик, а не плодят строки. После закрытия («Решено») следующее событие с тем же key открывает новый инцидент.

1. **Модель** `Incident` в app/db/models.py, таблица `incidents`:
   - id (BigInteger, PK);
   - kind (String(32) — значение AlertKind; строка, а не enum БД, чтобы новые виды не требовали миграции);
   - key (String(255));
   - message (Text — последний текст);
   - count (Integer, по умолчанию 1);
   - first_seen_at, last_seen_at (timezone-aware, не NULL);
   - resolved_at (timezone-aware, NULL — открыт), resolved_by (BigInteger, NULL — Telegram ID админа).
   - Индексы: частичный уникальный `uq_incidents_open_key` по key `WHERE resolved_at IS NULL` (не больше одного открытого инцидента на key) и `ix_incidents_resolved_last_seen` по (resolved_at, last_seen_at).
2. **Миграция** migrations/versions/0002_incidents.py (revision "0002", down_revision "0001"): создаёт таблицу и индексы, downgrade удаляет их.
3. **Репозиторий** app/db/repositories/incidents.py, `IncidentRepository(session)`:
   - `record(event: AlertEvent, max_message_chars: int) -> Incident` — один запрос `INSERT ... ON CONFLICT (key) WHERE resolved_at IS NULL DO UPDATE SET count = incidents.count + 1, message = excluded.message, last_seen_at = excluded.last_seen_at RETURNING *` (через `sqlalchemy.dialects.postgresql.insert`, `index_elements=["key"]`, `index_where=...`). Это без гонок при параллельных событиях. Время — event.created_at; message обрезается до max_message_chars по правилу truncate_words.
   - `get(incident_id) -> Incident | None`.
   - `list(open_only: bool, limit: int, offset: int) -> list[Incident]` — сначала самые свежие (last_seen_at DESC, id DESC).
   - `count(open_only: bool) -> int` и `count_open_by_kind() -> dict[str, int]`.
   - `resolve(incident_id, by: int, at: datetime) -> bool` — закрывает только открытый инцидент; уже закрытый или несуществующий -> False.
   - `resolve_all(by: int, at: datetime, kind: str | None = None) -> int` — закрывает все открытые (или только заданного вида) и возвращает количество.
   - `purge_resolved(before: datetime) -> int` — удаляет закрытые инциденты с resolved_at < before (открытые не трогает).
   - Экспортируй репозиторий из app/db/repositories/__init__.py так же, как остальные.

**Критерий готовности:** `alembic upgrade head` и `downgrade -1` работают; повтор события с тем же key увеличивает count у открытого инцидента; после resolve тот же key открывает новый инцидент.

**Граничные случаи:** Два одновременных record с одним key (две сессии, asyncio.gather) -> одна строка, count = 2; message длиннее лимита -> обрезан с «…»; resolve дважды -> второй раз False; resolve_all по виду не трогает другие виды; purge_resolved не удаляет открытые; limit <= 0 или offset < 0 -> ValueError.

**Тесты** (`tests/integration/test_repo_incidents.py`, маркер integration): все методы и граничные случаи выше; в `tests/integration/test_migrations.py` дописать проверку, что после upgrade есть таблица incidents с частичным уникальным индексом, а downgrade до 0001 её удаляет.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async), Alembic, PostgreSQL 16. Запись в журнал из AlertService — шаг 079; здесь только БД.

**Файлы:** app/db/models.py, migrations/versions/0002_incidents.py, app/db/repositories/incidents.py, app/db/repositories/__init__.py, tests/integration/test_repo_incidents.py, tests/integration/test_migrations.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Миграция 0001 не меняется. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Лимит длины сообщения — аргумент метода, ключ конфигурации для него добавит шаг 079.

**Формат ответа:** Полные файлы: app/db/models.py, migrations/versions/0002_incidents.py, app/db/repositories/incidents.py, app/db/repositories/__init__.py, tests/integration/test_repo_incidents.py, tests/integration/test_migrations.py. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): журнал ошибок — таблица incidents и репозиторий"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
