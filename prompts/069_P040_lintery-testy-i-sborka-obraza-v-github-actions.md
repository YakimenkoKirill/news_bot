# Шаг 069 из 070 · P040 · Этап 10 — Деплой, мониторинг, документация

**Задача трекера:** CI в GitHub Actions
**Результат:** .github/workflows/ci.yml
**Коммит:** `ci: линтеры, тесты и сборка образа в github actions`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/069_P040_lintery-testy-i-sborka-obraza-v-github-actions.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Напиши .github/workflows/ci.yml на push и pull_request: job lint (make install, make lint), job unit (make test), job integration с service postgres:16 и TEST_DATABASE_URL (make migrate, make test-integration), job docker (make docker-build). Python 3.11 через actions/setup-python с кэшем pip. Секреты не нужны: тесты не ходят во внешние API.

**Критерий готовности:** CI запускается на каждый push и PR и проходит на текущем коде.

**Граничные случаи:** Integration ждёт готовности postgres через health-cmd; сбой lint не мешает видеть результаты unit (отдельные jobs).

**Тесты** (`tests/unit/test_ci_workflow.py`): наличие четырёх jobs, триггеры, service postgres 16, вызываемые цели есть в Makefile.

**Контекст проекта:** GitHub Actions, Python 3.11.

**Файлы:** .github/workflows/ci.yml, tests/unit/test_ci_workflow.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: .github/workflows/ci.yml, tests/unit/test_ci_workflow.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "ci: линтеры, тесты и сборка образа в github actions"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
