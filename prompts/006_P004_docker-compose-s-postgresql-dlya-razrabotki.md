# Шаг 006 из 070 · P004 · Этап 1 — Каркас проекта

**Задача трекера:** Точка входа и docker-compose с PostgreSQL для разработки
**Результат:** docker/docker-compose.dev.yml с PostgreSQL 16 + цели Makefile
**Коммит:** `build(dev): docker-compose с PostgreSQL для разработки`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/006_P004_docker-compose-s-postgresql-dlya-razrabotki.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Создай docker/docker-compose.dev.yml: сервис postgres (образ postgres:16), пароль, пользователь и БД — из переменных ${POSTGRES_USER}, ${POSTGRES_PASSWORD}, ${POSTGRES_DB} файла .env, именованный volume, healthcheck через pg_isready, порт ${POSTGRES_PORT:-5432}. Добавь в Makefile цели db-up (с ожиданием healthy), db-down, db-logs. Дополни .env.example переменными POSTGRES_* и TEST_DATABASE_URL (для интеграционных тестов) так, чтобы DATABASE_URL указывал на этот контейнер через драйвер asyncpg.

**Критерий готовности:** make db-up поднимает PostgreSQL и ждёт healthy; python -m app стартует с DATABASE_URL из .env.example и пишет лог.

**Граничные случаи:** В compose нет захардкоженных паролей; повторный make db-up не пересоздаёт данные.

**Тесты** (`tests/unit/test_compose_dev.py`): образ postgres мажорной версии 16, есть healthcheck, пароль только через переменную, все переменные compose описаны в .env.example.

**Контекст проекта:** Docker Compose v2, PostgreSQL 16, PyYAML для теста.

**Файлы:** docker/docker-compose.dev.yml, Makefile, .env.example, tests/unit/test_compose_dev.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: docker/docker-compose.dev.yml, Makefile, .env.example, tests/unit/test_compose_dev.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "build(dev): docker-compose с PostgreSQL для разработки"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
