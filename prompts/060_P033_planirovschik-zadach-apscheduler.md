# Шаг 060 из 070 · P033 · Этап 8 — Бот управления, модерация и сборка приложения

**Задача трекера:** Планировщик и сборка приложения
**Результат:** app/scheduler.py: регистрация задач
**Коммит:** `feat(app): планировщик задач apscheduler`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/060_P033_planirovschik-zadach-apscheduler.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй build_scheduler(deps, cfg) -> AsyncIOScheduler с задачами: run_collection_cycle, process_new_batch, process_normalized_batch, publish_due, check_stale, resend_due, heartbeat (запись heartbeat_at в настройки). Для всех: max_instances=1, coalesce=True, misfire_grace_time из конфигурации. Обёртка задачи логирует старт и итог с job id, ловит исключения и после failures_before_alert подряд отправляет алерт job_failed.

**Критерий готовности:** Все стадии пайплайна запускаются по расписанию и не перекрываются.

**Граничные случаи:** Исключение задачи не останавливает планировщик; успешный запуск сбрасывает счётчик ошибок; интервалы берутся только из конфигурации.

**Тесты** (`tests/unit/test_scheduler.py`): состав и интервалы задач, параметры max_instances и coalesce, обработка исключения и алерт после серии ошибок.

**Контекст проекта:** Python 3.11+, APScheduler 3.x.

**Файлы:** app/scheduler.py, tests/unit/test_scheduler.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: scheduler.collect_every_s = 60; scheduler.normalize_every_s = 30; scheduler.ai_every_s = 30; scheduler.publish_every_s = 30; scheduler.stale_every_min = 30; scheduler.review_every_min = 5; scheduler.heartbeat_every_s = 60; scheduler.misfire_grace_s = 60; scheduler.failures_before_alert = 3.

**Формат ответа:** Полные файлы: app/scheduler.py, tests/unit/test_scheduler.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(app): планировщик задач apscheduler"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
