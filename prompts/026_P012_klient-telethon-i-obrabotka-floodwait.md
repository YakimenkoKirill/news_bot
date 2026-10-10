# Шаг 026 из 070 · P012 · Этап 4 — Сборщик Telegram-каналов

**Задача трекера:** Telethon-клиент и обработка FloodWait
**Результат:** Telethon-клиент, вход по сессии и обработка FloodWait
**Коммит:** `feat(telegram): клиент telethon и обработка floodwait`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/026_P012_klient-telethon-i-obrabotka-floodwait.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/collectors/telegram/client.py: build_client(settings) создаёт TelegramClient с сессией из telethon_session_path; connect_authorized(client) подключается и, если сессия не авторизована, бросает TelegramAuthRequired с подсказкой запустить scripts/telethon_login.py (интерактивного входа в приложении нет); with_flood_wait(call_factory) выполняет вызов и при FloodWaitError ждёт seconds + flood_wait_extra_s, повторяя не больше flood_wait_max_retries; ожидание дольше flood_wait_max_s -> TemporaryCollectorError. scripts/telethon_login.py — разовый интерактивный вход для создания файла сессии.

**Критерий готовности:** Приложение подключается только по готовой сессии; FloodWait обрабатывается ожиданием, а не повторными запросами.

**Граничные случаи:** Неавторизованная сессия -> TelegramAuthRequired; превышение числа повторов -> TemporaryCollectorError; api_hash не попадает в логи.

**Тесты** (`tests/unit/test_telegram_client.py`): ожидание при FloodWait, лимит повторов, слишком долгое ожидание, неавторизованная сессия (моки Telethon, подменённый asyncio.sleep).

**Контекст проекта:** Python 3.11+, Telethon.

**Файлы:** app/collectors/telegram/client.py, scripts/telethon_login.py, tests/unit/test_telegram_client.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Не входи в аккаунт и не создавай файл сессии сам — это делает человек через scripts/telethon_login.py. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: telegram.flood_wait_extra_s = 1; telegram.flood_wait_max_retries = 3; telegram.flood_wait_max_s = 300.

**Формат ответа:** Полные файлы: app/collectors/telegram/client.py, scripts/telethon_login.py, tests/unit/test_telegram_client.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(telegram): клиент telethon и обработка floodwait"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
