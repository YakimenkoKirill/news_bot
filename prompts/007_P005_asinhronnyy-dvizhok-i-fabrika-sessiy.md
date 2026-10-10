# Шаг 007 из 070 · P005 · Этап 2 — База данных

**Задача трекера:** Описать модели данных SQLAlchemy 2.0
**Результат:** app/db/base.py: движок, фабрика сессий, Base
**Коммит:** `feat(db): асинхронный движок и фабрика сессий`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/007_P005_asinhronnyy-dvizhok-i-fabrika-sessiy.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/db/base.py: Base (DeclarativeBase) с MetaData и naming_convention для pk, fk, uq, ix, ck; create_engine(settings) -> AsyncEngine с pool_size, max_overflow, pool_pre_ping из конфигурации; create_session_factory(engine) -> async_sessionmaker(expire_on_commit=False); session_scope(factory) — асинхронный контекстный менеджер: commit при успехе, rollback и повторный raise при исключении.

**Критерий готовности:** Движок создаётся из настроек; session_scope коммитит при успехе и откатывает при ошибке.

**Граничные случаи:** Исключение внутри session_scope пробрасывается без подмены; ошибка commit тоже вызывает rollback; naming_convention даёт детерминированные имена индексов.

**Тесты** (`tests/unit/test_db_base.py`): naming_convention, параметры пула из конфигурации, commit при успехе, rollback и raise при ошибке (сессия — мок).

**Контекст проекта:** Python 3.11+, SQLAlchemy 2.0 async, asyncpg.

**Файлы:** app/db/base.py, tests/unit/test_db_base.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: db.pool_size = 5, db.max_overflow = 5, db.echo = false.

**Формат ответа:** Полные файлы: app/db/base.py, tests/unit/test_db_base.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): асинхронный движок и фабрика сессий"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
