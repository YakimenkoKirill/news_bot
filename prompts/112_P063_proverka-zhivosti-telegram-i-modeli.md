# Шаг 112 из 113 · P063 · Этап 19 — Настройки и эксплуатация

**Задача трекера:** Проверка живости по компонентам
**Результат:** Проверка живости видит отключённый Telethon и неработающую модель, а не только БД и heartbeat
**Коммит:** `feat(health): проверка Telethon и модели`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/112_P063_proverka-zhivosti-telegram-i-modeli.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Сейчас `python -m app.health` проверяет только БД и heartbeat планировщика. Если Telethon отключился или модель перестала отвечать, приложение считается здоровым.

1. **Метки в настройках** (таблица settings):
   - `tg_alive_at` — TelegramCollector пишет время раз в health.tg_beat_s, если `client.is_connected()`; при отключении не пишет;
   - `ai_ok_at` — стадия ИИ пишет время после каждого успешного ответа модели.
2. **Правила** (app/health.py, чистые функции `evaluate_tg` и `evaluate_ai` по образцу evaluate_heartbeat):
   - Telegram: есть хотя бы один включённый tg-источник и `tg_alive_at` старше health.tg_max_silence_s -> проблема «Telethon не на связи»;
   - модель: есть новости в normalized старше health.ai_backlog_min минут и `ai_ok_at` старше того же срока -> проблема «модель не обрабатывает очередь». Пауза (настройка paused) и исчерпанный бюджет — не проблема (проверь настройку paused и сумму ai_requests за сутки против ai.daily_budget).
   - В первые health.startup_grace_s после старта проверки не срабатывают (как heartbeat).
3. **Алерты:** те же условия раз в scheduler.health_every_min в процессе приложения -> алерты новых видов `tg_down` и `ai_stalled` (журнал ошибок, шаблоны в texts.ALERT_TEMPLATES, названия в ALERT_KIND_LABELS).
4. **README.md:** что проверяет healthcheck и как реагировать на каждую проблему.

**Критерий готовности:** Docker healthcheck становится unhealthy, а админ получает алерт, если Telethon отключился или модель перестала обрабатывать очередь; пауза и исчерпанный бюджет ложных срабатываний не дают.

**Граничные случаи:** Нет tg-источников -> Telegram не проверяется; очередь normalized пуста -> модель не проверяется, сколько бы ни прошло времени; пауза -> не проблема; бюджет исчерпан -> не проблема; метки ещё нет сразу после старта -> grace; восстановление -> healthcheck снова healthy, инцидент можно закрыть в /errors.

**Тесты:** `tests/unit/test_health.py` — evaluate_tg и evaluate_ai для всех случаев; `tests/unit/test_telegram_collector.py` — метка пишется только при подключении; `tests/unit/test_stage_ai.py` — ai_ok_at после успеха; `tests/unit/test_alerts.py` — шаблоны новых видов; `tests/unit/test_scheduler.py` — задача; `tests/integration/test_health_db.py` — check() с реальной БД.

**Контекст проекта:** Python 3.11+, APScheduler, PostgreSQL 16. Docker healthcheck уже вызывает `python -m app.health`.

**Файлы:** app/health.py, app/collectors/telegram/collector.py, app/pipeline/stage_ai.py, app/domain/alerts.py, app/bot/texts.py, app/scheduler.py, app/main.py, app/config.py, configs/base.yaml, README.md, тестовые файлы из списка выше.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). Смена статуса новости — только через set_status и app/domain/status.py. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Если меняется состав или порядок компонентов в app/main.py — обнови ожидания tests/unit/test_app_wiring.py; новая секция конфигурации должна быть описана в README.md (это проверяет tests/unit/test_readme.py). Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: health.tg_beat_s = 60, health.tg_max_silence_s = 600, health.ai_backlog_min = 30, scheduler.health_every_min = 5.

**Формат ответа:** Полные файлы из списка «Файлы»; для README.md — новый или изменённый раздел целиком. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(health): проверка Telethon и модели"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
