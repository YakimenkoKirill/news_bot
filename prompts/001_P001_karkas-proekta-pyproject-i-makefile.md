# Шаг 001 из 070 · P001 · Этап 1 — Каркас проекта

**Задача трекера:** Создать репозиторий, структуру пакета, pyproject, линтеры и Makefile
**Результат:** pyproject.toml + каркас пакета app/ + Makefile + pre-commit + .gitignore
**Коммит:** `chore: каркас проекта, pyproject и Makefile`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/001_P001_karkas-proekta-pyproject-i-makefile.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Создай каркас проекта. pyproject.toml (build-backend hatchling, имя пакета news-bot, requires-python >=3.11). Пакет app/ с app/__init__.py (__version__ = `0.1.0`) и пустыми __init__.py подпакетов: app/domain, app/db, app/db/repositories, app/collectors, app/collectors/telegram, app/pipeline, app/pipeline/ai, app/publisher, app/bot, app/bot/handlers. Runtime-зависимости: aiogram>=3, telethon>=1, aiohttp>=3, feedparser>=6, beautifulsoup4>=4, lxml>=5, trafilatura>=1, sqlalchemy[asyncio]>=2, asyncpg>=0.29, alembic>=1, apscheduler>=3.10,<4, tenacity>=8, simhash>=2, openai>=1, pydantic>=2, pydantic-settings>=2, PyYAML>=6, structlog>=24. Dev-зависимости (optional-dependencies dev): pytest, pytest-asyncio, pytest-cov, aioresponses, ruff, mypy, pre-commit, types-PyYAML. Настройки ruff (line-length 100, правила E, F, I, B, UP, SIM, ASYNC, RUF; RUF001, RUF002, RUF003 отключить — в проекте русские строки), mypy strict, pytest (asyncio_mode = auto, маркер integration, filterwarnings = error); в ruff исключи каталог prompts/ (extend-exclude). Makefile с целями install, fmt, lint (ruff check, ruff format --check, mypy app tests), test (pytest -m `not integration`), test-integration (pytest -m integration). .pre-commit-config.yaml с ruff и mypy. .gitignore для Python, .env, data/, backups/.

**Критерий готовности:** make install && make lint && make test проходят на чистом клоне; mypy strict без ошибок; все подпакеты импортируются.

**Граничные случаи:** Пустые пакеты не ломают mypy; маркер integration зарегистрирован, поэтому PytestUnknownMarkWarning не превращается в ошибку; .env и data/ не попадают в git.

**Тесты** (`tests/unit/test_smoke.py`): импорт каждого подпакета; __version__ — строка формата X.Y.Z.

**Контекст проекта:** Python 3.11+, hatchling, ruff, mypy, pytest.

**Файлы:** pyproject.toml, Makefile, .pre-commit-config.yaml, .gitignore, app/__init__.py, app/*/__init__.py (все подпакеты), tests/__init__.py, tests/unit/test_smoke.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Это первый шаг: репозиторий может быть пустым. Версии зависимостей — только нижние границы мажорных версий, точные пины не выдумывай. Заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: pyproject.toml, Makefile, .pre-commit-config.yaml, .gitignore, app/__init__.py, app/*/__init__.py (все подпакеты), tests/__init__.py, tests/unit/test_smoke.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "chore: каркас проекта, pyproject и Makefile"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
