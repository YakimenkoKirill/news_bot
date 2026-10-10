# Шаг 066 из 070 · P037 · Этап 10 — Деплой, мониторинг, документация

**Задача трекера:** Docker-образ и боевой docker-compose
**Результат:** docker/docker-compose.prod.yml + entrypoint с миграциями
**Коммит:** `build(docker): боевой docker-compose и entrypoint`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/066_P037_boevoy-docker-compose-i-entrypoint.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Напиши docker/docker-compose.prod.yml: сервисы app (образ из Dockerfile, env_file .env, volume data для Telethon-сессии, restart unless-stopped, depends_on postgres condition service_healthy, healthcheck `python -m app.health`) и postgres:16 (volume pgdata, healthcheck pg_isready, порт наружу не публикуется). docker/entrypoint.sh: set -euo pipefail, alembic upgrade head, exec python -m app.

**Критерий готовности:** docker compose -f docker/docker-compose.prod.yml up -d поднимает приложение после готовности БД.

**Граничные случаи:** Миграции применяются при каждом старте и идемпотентны; PostgreSQL недоступен снаружи; ошибка миграции останавливает контейнер с ненулевым кодом.

**Тесты** (`tests/unit/test_compose_prod.py`): restart policy, depends_on с healthy, нет ports у postgres, volumes, healthcheck app; entrypoint содержит set -euo pipefail и exec.

**Контекст проекта:** Docker Compose v2, PostgreSQL 16, bash.

**Файлы:** docker/docker-compose.prod.yml, docker/entrypoint.sh, tests/unit/test_compose_prod.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: docker/docker-compose.prod.yml, docker/entrypoint.sh, tests/unit/test_compose_prod.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "build(docker): боевой docker-compose и entrypoint"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
