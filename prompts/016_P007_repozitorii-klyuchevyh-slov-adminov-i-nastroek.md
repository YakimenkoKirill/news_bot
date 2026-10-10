# Шаг 016 из 070 · P007 · Этап 2 — База данных

**Задача трекера:** Репозитории доступа к данным
**Результат:** Репозитории Keyword, Admin, Setting
**Коммит:** `feat(db): репозитории ключевых слов, админов и настроек`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/016_P007_repozitorii-klyuchevyh-slov-adminov-i-nastroek.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/db/repositories/misc.py: KeywordRepository (add(word, type) с нормализацией lower и ё->е, list_by_type, delete), AdminRepository (is_admin, add, list), SettingsRepository (get(key, default), set(key, value) через upsert ON CONFLICT DO UPDATE).

**Критерий готовности:** Методы работают на реальной БД; повторное добавление слова не создаёт дубль.

**Граничные случаи:** get отсутствующего ключа возвращает default; set перезаписывает значение; слово с пробелами по краям обрезается; пустое слово -> ValueError.

**Тесты** (`tests/integration/test_repo_misc.py`): нормализация и уникальность слов, is_admin, upsert настроек, default.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2.0 async, PostgreSQL 16.

**Файлы:** app/db/repositories/misc.py, tests/integration/test_repo_misc.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/db/repositories/misc.py, tests/integration/test_repo_misc.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/). Команды проверки: make db-up && make test-integration.

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): репозитории ключевых слов, админов и настроек"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
