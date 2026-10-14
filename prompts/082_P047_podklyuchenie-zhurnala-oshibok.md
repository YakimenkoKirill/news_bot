# Шаг 082 из 082 · P047 · Этап 12 — Журнал ошибок в боте управления

**Задача трекера:** Просмотр ошибок в боте управления
**Результат:** Журнал ошибок подключён: запись из AlertService, /errors в меню, счётчик в /status, очистка старых записей, README и сквозной тест
**Коммит:** `feat(app): журнал ошибок и команда /errors`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/082_P047_podklyuchenie-zhurnala-oshibok.md». После коммита: `python prompts/next_step.py --done`.

## Задача

1. **app/main.py:**
   - адаптер `_SessionIncidentStore(scope, max_message_chars)` — в каждом record открывает свою транзакцию и вызывает IncidentRepository.record (по образцу `_SessionCost` и `_SessionSourceHealth`);
   - функция чтения заглушённых видов: настройка `alerts_muted` из SettingsRepository -> set[AlertKind] (неизвестные значения пропускаются с предупреждением в логе);
   - AlertService создаётся с `store=...`, `muted=...`, `silent_kinds=frozenset(settings.alerts.silent_kinds)`;
   - роутер `errors_handlers.create_router()` и `"errors_deps": ErrorsDeps(...)` в workflow_data.
2. **Очистка журнала:** новая задача планировщика `purge_incidents` раз в scheduler.purge_every_h часов удаляет закрытые инциденты старше alerts.retention_days (`purge_resolved(now - retention_days)`). Открытые не удаляются никогда. Добавь поле в SchedulerDeps и регистрацию в build_scheduler по образцу check_stale; сбой задачи считается как у остальных (job_failed).
3. **/status** (app/bot/handlers/status.py): строка «Открытых ошибок: N — /errors» (count(open_only=True)); при N = 0 — «Открытых ошибок нет». StatusDeps получает incident_repo.
4. **texts.COMMANDS:** после "queue" добавь `"errors": "журнал ошибок, упавшие новости"`.
5. **README.md:** раздел «Ошибки и алерты»: что пишется в журнал (все события, включая повторы и тихие виды), чем открытый инцидент отличается от закрытого, как работают «Решено», «Решить все», «🔕 Не присылать в чат» и alerts.silent_kinds, что делает «Повторить» для разных причин, сколько хранится журнал.
6. **Конфигурация:** alerts.retention_days = 30, scheduler.purge_every_h = 24 — в configs/base.yaml и в моделях AlertsConfig и SchedulerConfig.

**Критерий готовности:** После запуска `python -m app` любая ошибка (сбой источника, модели, публикации, фоновой задачи) видна в /errors с числом повторов, даже если сообщение в чат было подавлено или вид заглушён; /errors есть в меню; /status показывает число открытых ошибок; make lint, make test и make test-integration проходят.

**Граничные случаи:** БД недоступна -> алерт job_failed всё равно приходит в чат (store падает, отправка продолжается); вид заглушён через /errors -> следующий алерт этого вида записан, но не прислан, без перезапуска; не-админ не видит /errors и не может нажать кнопки er:... (middleware доступа); purge не трогает открытые инциденты и закрытые моложе retention_days.

**Тесты:**
- `tests/integration/test_errors_journal.py` (маркер integration; бот — fake, как в tests/integration/test_pipeline.py): сбой задачи 3 раза подряд -> один открытый инцидент job_failed; ещё повтор внутри throttle -> count растёт, нового сообщения нет; resolve -> следующий сбой открывает новый инцидент; заглушённый вид -> запись без сообщения; purge удаляет только старые закрытые; упавшая публикация -> инцидент publish_error и новость в list_failed, retry_failed возвращает её в approved.
- `tests/unit/test_scheduler.py` — регистрация и интервал purge_incidents.
- `tests/unit/test_bot_status.py` — строка об открытых ошибках (оба варианта).
- `tests/unit/test_bot_middleware.py` — отказ не-админу для /errors и callback er:....
- `tests/unit/test_config.py` — новые ключи.

**Контекст проекта:** Python 3.11+, aiogram 3.x, APScheduler, PostgreSQL 16. Последний шаг этапа 12.

**Файлы:** app/main.py, app/scheduler.py, app/bot/handlers/status.py, app/bot/texts.py, configs/base.yaml, app/config.py, README.md, tests/integration/test_errors_journal.py, tests/unit/test_scheduler.py, tests/unit/test_bot_status.py, tests/unit/test_bot_middleware.py, tests/unit/test_config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: alerts.retention_days = 30, scheduler.purge_every_h = 24. Раздел «Автопилот» в CLAUDE.md и файлы prompts/ не меняй.

**Формат ответа:** Полные файлы: app/main.py, app/scheduler.py, app/bot/handlers/status.py, app/bot/texts.py, configs/base.yaml, app/config.py, тестовые файлы из списка выше; для README.md — новый раздел целиком. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(app): журнал ошибок и команда /errors"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
