# Шаг 065 из 070 · P037 · Этап 10 — Деплой, мониторинг, документация

**Задача трекера:** Docker-образ и боевой docker-compose
**Результат:** Dockerfile (multi-stage) + .dockerignore
**Коммит:** `build(docker): многоступенчатый dockerfile`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/065_P037_mnogostupenchatyy-dockerfile.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Напиши Dockerfile: стадия builder на python:3.11-slim собирает wheel-пакеты зависимостей, стадия runtime на python:3.11-slim ставит их без кэша pip, создаёт пользователя с uid 10001, WORKDIR /app, копирует app/, migrations/, alembic.ini, configs/, scripts/, docker/entrypoint.sh, USER 10001, CMD [`python`, `-m`, `app`]. .dockerignore исключает .env, .git, data/, backups/, tests/, кэши. Цель Makefile docker-build.

**Критерий готовности:** make docker-build собирает образ; контейнер запускается не от root.

**Граничные случаи:** В образ не попадают .env и секреты; повторная сборка без изменений зависимостей использует кэш слоя зависимостей.

**Тесты** (`tests/unit/test_dockerfile.py`): последний USER не root, нет COPY .env, .dockerignore содержит .env и data/, образ python мажорной версии 3.11.

**Контекст проекта:** Docker, Python 3.11.

**Файлы:** Dockerfile, .dockerignore, Makefile, tests/unit/test_dockerfile.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: Dockerfile, .dockerignore, Makefile, tests/unit/test_dockerfile.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/). Команды проверки: make docker-build.

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "build(docker): многоступенчатый dockerfile"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
