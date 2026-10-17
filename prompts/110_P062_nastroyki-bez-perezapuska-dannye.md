# Шаг 110 из 113 · P062 · Этап 19 — Настройки и эксплуатация

**Задача трекера:** Настройки без перезапуска
**Результат:** RuntimeConfig: значения ключевых параметров из БД поверх yaml, применяются без перезапуска
**Коммит:** `feat(config): изменяемые на лету параметры поверх yaml`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/110_P062_nastroyki-bez-perezapuska-dannye.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Тема канала, порог оценки, интервал, тихие часы и лимит источника сейчас меняются только правкой yaml с перезапуском.

1. **Список изменяемых параметров** (`RUNTIME_KEYS` в app/runtime_config.py) с типом и проверкой — той же, что в pydantic-модели секции: `channel.topic` (str, 3–200 символов), `ai.min_score` (0..1), `publisher.min_interval_min` (≥ 1), `publisher.quiet_hours` (две строки ЧЧ:ММ или пусто), `publisher.max_queued_per_source` (≥ 0), `publisher.score_decay_per_h` (≥ 0).
2. **Хранение:** настройка `runtime_overrides` (dict ключ -> значение) в существующей таблице settings.
3. **`RuntimeConfig`:** `async snapshot() -> RuntimeSnapshot` — секции channel, ai, publisher с применёнными переопределениями (`model_copy(update=...)` + повторная валидация); кэш на runtime.cache_s секунд; `async set(key, raw_value, by: int) -> None` (валидация, запись, сброс кэша, лог `runtime.set` с ключом, старым и новым значением и админом); `async reset(key, by)`. Неизвестный ключ или неверное значение -> ValueError с понятным текстом.
4. **Потребители читают снимок на каждом запуске**, а не держат cfg с момента старта:
   - стадия ИИ: ChannelProfile (topic) и min_score — снимок в начале пачки;
   - publish_due и recover: min_interval_min, quiet_window, score_decay_per_h;
   - QueueAdmission и trim_queue: max_queued_per_source и score_decay_per_h;
   - /queue (ETA и заполненность) и /status.
   Передавай в deps поле `runtime: RuntimeConfig | None = None`; если None — используется статичный cfg (так существующие тесты не меняются).
5. **README.md:** секция конфигурации `runtime` (tests/unit/test_readme.py требует описания каждой секции) — коротко, подробности команды /settings добавит шаг 111.
6. Значение из yaml остаётся значением по умолчанию; переопределение в БД важнее; ENV-переменные по-прежнему задают yaml-значения при старте.

**Критерий готовности:** Изменение параметра через RuntimeConfig.set влияет на ближайший запуск соответствующей задачи (не позже runtime.cache_s) без перезапуска приложения.

**Граничные случаи:** Неверное значение -> ValueError, в БД ничего не записано; переопределение удалено -> значение из yaml; повреждённое значение в БД (ручная правка) -> лог error, ключ игнорируется, используется yaml; изменение quiet_hours посреди тихих часов -> следующий слот считается по новым; уменьшение max_queued_per_source -> лишнее вытесняет trim_queue при следующем допуске или старте (вызови trim_queue сразу после set этого ключа).

**Тесты:** `tests/unit/test_runtime_config.py` — валидация каждого ключа, кэш и его сброс, повреждённое значение, reset; `tests/integration/test_runtime_config_db.py` — запись и чтение через SettingsRepository; дописать в тесты стадии ИИ, publish_due, QueueAdmission и /queue случай с runtime.

**Контекст проекта:** Python 3.11+, pydantic 2. Команда /settings — шаг 111.

**Файлы:** app/runtime_config.py, app/pipeline/stage_ai.py, app/publisher/stage_publish.py, app/pipeline/queue_limit.py, app/bot/handlers/queue.py, app/bot/handlers/status.py, app/main.py, app/config.py, configs/base.yaml, README.md, тестовые файлы из списка выше.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). Смена статуса новости — только через set_status и app/domain/status.py. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Если меняется состав или порядок компонентов в app/main.py — обнови ожидания tests/unit/test_app_wiring.py; новая секция конфигурации должна быть описана в README.md (это проверяет tests/unit/test_readme.py). Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: runtime.cache_s = 30.

**Формат ответа:** Полные файлы из списка «Файлы»; для README.md — новый или изменённый раздел целиком. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(config): изменяемые на лету параметры поверх yaml"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
