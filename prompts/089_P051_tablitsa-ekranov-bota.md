# Шаг 089 из 096 · P051 · Этап 14 — Один экран в чате бота

**Задача трекера:** Один экран: хранение и учёт сообщений
**Результат:** Таблица bot_screens (миграция 0004) и ScreenRepository
**Коммит:** `feat(db): таблица текущих экранов бота`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/089_P051_tablitsa-ekranov-bota.md». После коммита: `python prompts/next_step.py --done`.

## Задача

В чате админа с ботом должен висеть только текущий «экран» — группа сообщений, отправленных в ответ на одно событие (например, заметка модерации и черновик, отчёт из нескольких частей). Новый экран удаляет прошлый. Чтобы после перезапуска приложения бот всё равно знал, что удалять, текущий экран каждого чата хранится в БД.

1. **Модель** `BotScreen` в app/db/models.py, таблица `bot_screens`:
   - chat_id (BigInteger, PK);
   - screen_token (String(64)) — номер экрана, которому принадлежат сообщения;
   - message_ids (ARRAY(BigInteger), не NULL, по умолчанию пустой массив);
   - updated_at (timezone-aware, server_default now()).
2. **Миграция** migrations/versions/0004_bot_screens.py (revision "0004", down_revision "0003"): создаёт таблицу, downgrade удаляет.
3. **Репозиторий** app/db/repositories/screens.py, `ScreenRepository(session)`:
   - `register(chat_id, token, message_ids: Sequence[int], at: datetime) -> list[int]` — регистрирует отправленные сообщения и возвращает id сообщений, которые теперь нужно удалить. Блокирует строку чата (`SELECT ... FOR UPDATE`), затем: строки нет -> создать с этими id и вернуть []; token совпадает с сохранённым -> дописать id (без повторов) и вернуть []; token другой -> вернуть старые id, а в строку записать новый token и новые id. Одна транзакция, без гонок между параллельными вызовами.
   - `get(chat_id) -> BotScreen | None`.
   - `forget(chat_id, message_ids: Sequence[int]) -> None` — убрать id из текущего экрана (сообщение удалено вручную или не существует).
   - Экспорт из app/db/repositories/__init__.py.

**Критерий готовности:** `alembic upgrade head` и `downgrade -1` работают; register возвращает ровно те сообщения, которые нужно удалить, при любом порядке вызовов.

**Граничные случаи:** Первое сообщение в чате -> удалять нечего; несколько сообщений одного экрана подряд -> удалять нечего, все в message_ids; новый экран -> возвращается весь прошлый экран; два параллельных register разных экранов одного чата (две сессии, asyncio.gather) -> ни одно сообщение не «теряется»: каждое из них либо в текущем экране, либо в списке на удаление; повторная регистрация того же message_id не дублирует его; пустой message_ids -> ValueError.

**Тесты:** `tests/integration/test_repo_screens.py` (маркер integration) — все случаи выше; `tests/integration/test_migrations.py` — таблица есть после upgrade и удаляется downgrade до 0003.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async), Alembic, PostgreSQL 16. Удаление сообщений и перехват отправок — шаги 090–092.

**Файлы:** app/db/models.py, migrations/versions/0004_bot_screens.py, app/db/repositories/screens.py, app/db/repositories/__init__.py, tests/integration/test_repo_screens.py, tests/integration/test_migrations.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Миграции 0001–0003 не меняются. Новые зависимости, ключи конфигурации, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/db/models.py, migrations/versions/0004_bot_screens.py, app/db/repositories/screens.py, app/db/repositories/__init__.py, tests/integration/test_repo_screens.py, tests/integration/test_migrations.py. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): таблица текущих экранов бота"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
