# Шаг 113 из 113 · P064 · Этап 19 — Настройки и эксплуатация

**Задача трекера:** Надёжность CI
**Результат:** В CI интеграционные тесты не могут молча пропуститься, а миграции проверяются вверх и вниз
**Коммит:** `ci: обязательная БД для интеграционных тестов и проверка миграций`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/113_P064_strogie-integratsionnye-testy-v-ci.md». После коммита: `python prompts/next_step.py --done`.

## Задача

CI уже поднимает PostgreSQL и запускает `make test-integration`. Но tests/integration/conftest.py пропускает тесты (pytest.skip), если TEST_DATABASE_URL не задан: опечатка в workflow превратит все 22 файла в «skipped», и CI останется зелёным.

1. **conftest:** если задана переменная окружения `REQUIRE_TEST_DB=1`, отсутствие TEST_DATABASE_URL или недоступная БД — ошибка (pytest.fail / pytest.exit с ненулевым кодом), а не skip. Переменную окружения читай через app/config.py (`get_test_database_url` уже там — добавь рядом `require_test_db()`), а не os.environ в тестах.
2. **Makefile:** цель `test-integration-ci` = `REQUIRE_TEST_DB=1 pytest -m integration -rs` и проверка, что не было пропусков: `--strict-markers` и выход с ошибкой при skipped > 0 (через плагин-хук в conftest: `pytest_terminal_summary` или `pytest_sessionfinish` при REQUIRE_TEST_DB).
3. **Миграции:** цель `check-migrations` = `alembic upgrade head && alembic downgrade base && alembic upgrade head` на пустой БД; плюс проверка, что модели совпадают со схемой (`alembic check` или сравнение через `compare_metadata` в отдельном тесте).
4. **.github/workflows/ci.yml:** в job integration — `make check-migrations` и `make test-integration-ci` вместо `make migrate` и `make test-integration`.
5. **README.md:** раздел о тестах — новые цели и зачем они.

**Критерий готовности:** Если в CI интеграционные тесты не смогли подключиться к БД, job падает; любое расхождение моделей и миграций или нерабочий downgrade ломает CI.

**Граничные случаи:** Локально без REQUIRE_TEST_DB — поведение прежнее (skip с подсказкой); БД есть, но миграции не применены -> тесты сами применяют их фикстурой (как сейчас); новый индекс в модели без миграции -> check-migrations падает.

**Тесты:** `tests/unit/test_ci_workflow.py` — обнови проверки под новые цели в job integration (остальные проверки не ослабляй); `tests/unit/test_ci_guard.py` — require_test_db и логика подсчёта пропусков (через pytester, если он доступен в проекте, иначе на уровне функций); `tests/integration/test_schema_matches_models.py` — compare_metadata без расхождений. Прогони `make check-migrations` и `make test-integration-ci` локально с поднятой БД (make db-up).

**Контекст проекта:** Python 3.11+, pytest, Alembic, GitHub Actions.

**Файлы:** tests/integration/conftest.py, app/config.py, Makefile, .github/workflows/ci.yml, README.md, tests/unit/test_ci_workflow.py, tests/unit/test_ci_guard.py, tests/integration/test_schema_matches_models.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). Смена статуса новости — только через set_status и app/domain/status.py. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Если меняется состав или порядок компонентов в app/main.py — обнови ожидания tests/unit/test_app_wiring.py; новая секция конфигурации должна быть описана в README.md (это проверяет tests/unit/test_readme.py).

**Формат ответа:** Полные файлы из списка «Файлы»; для README.md — новый или изменённый раздел целиком. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "ci: обязательная БД для интеграционных тестов и проверка миграций"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
