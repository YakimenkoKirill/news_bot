# Шаг 003 из 070 · P002 · Этап 1 — Каркас проекта

**Задача трекера:** Настроить конфигурацию через pydantic-settings и .env
**Результат:** app/config.py + .env.example + configs/base.yaml
**Коммит:** `feat(config): settings на pydantic-settings и .env.example`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/003_P002_settings-na-pydantic-settings-i-env-example.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/config.py на pydantic-settings и файл .env.example. Класс Settings читает .env и configs/{ENV}.yaml поверх configs/base.yaml (глубокое слияние, dev/prod переопределяют base); ENV берётся из переменной окружения, по умолчанию dev. load_settings(configs_dir, env) — без кэша (для тестов), get_settings() возвращает singleton (lru_cache). Поля из окружения: env, database_url, telegram_bot_token (SecretStr), telegram_channel_id (int), telegram_admin_ids (list[int] — белый список админов бота), telethon_api_id (int), telethon_api_hash (SecretStr), telethon_session_path (по умолчанию data/telethon), deepseek_api_key (SecretStr), deepseek_base_url (по умолчанию https://api.deepseek.com), log_level (по умолчанию INFO). Параметры из yaml — вложенные pydantic-модели с extra=forbid; на этом шаге секция app (timezone — задаёт человек).

**Критерий готовности:** Settings читает .env и yaml со слиянием; переменная окружения важнее yaml; get_settings() возвращает один и тот же объект; секреты не видны в repr(Settings).

**Граничные случаи:** Нет обязательной переменной -> ValidationError с именем поля; неизвестный ENV -> ValueError; лишний ключ в yaml -> ValidationError; пустой список telegram_admin_ids -> ValidationError.

**Тесты** (`tests/unit/test_config.py`): слияние yaml, приоритет переменной окружения над yaml, singleton, отсутствие обязательной переменной, неизвестный ENV, лишний ключ, секрет не в repr.

**Контекст проекта:** Python 3.11+, pydantic 2.x, pydantic-settings, PyYAML.

**Файлы:** app/config.py, .env.example, configs/base.yaml, configs/dev.yaml, configs/prod.yaml, tests/unit/test_config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: app.timezone — задаёт человек (IANA-имя, например Europe/Moscow).

**Формат ответа:** Полные файлы: app/config.py, .env.example, configs/base.yaml, configs/dev.yaml, configs/prod.yaml, tests/unit/test_config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(config): settings на pydantic-settings и .env.example"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
