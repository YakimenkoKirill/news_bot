# Шаг 024 из 070 · P011 · Этап 3 — Сборщики: RSS и сайты

**Задача трекера:** Здоровье источников и цикл сбора
**Результат:** app/collectors/health.py + app/domain/alerts.py
**Коммит:** `feat(collectors): отслеживание здоровья источников`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/024_P011_otslezhivanie-zdorovya-istochnikov.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Создай app/domain/alerts.py: AlertEvent (kind: source_down | source_stale | ai_down | budget_exceeded | publish_error | job_failed, key, message, created_at) и протокол AlertSink (async emit(event)). Реализуй app/collectors/health.py: SourceHealthTracker(repo, sink) — on_success(source, items_count, now) сбрасывает ошибки и обновляет last_item_at при новых новостях; on_error(source, error) увеличивает счётчик и при достижении max_consecutive_errors отправляет source_down ровно один раз до восстановления; check_stale(sources, now) отправляет source_stale для источников без новостей дольше stale_after_h.

**Критерий готовности:** Алерты отправляются по порогам из конфигурации и не повторяются без смены состояния.

**Граничные случаи:** Восстановленный источник после новой серии ошибок снова даёт алерт; новый источник без last_item_at не считается устаревшим раньше stale_after_h от created_at.

**Тесты** (`tests/unit/test_source_health.py`): порог ошибок, однократность алерта, сброс после успеха, stale по времени, новый источник (фейковые repo и sink).

**Контекст проекта:** Python 3.11+, pydantic 2.x.

**Файлы:** app/domain/alerts.py, app/collectors/health.py, tests/unit/test_source_health.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: collectors.max_consecutive_errors = 5; collectors.stale_after_h = 6.

**Формат ответа:** Полные файлы: app/domain/alerts.py, app/collectors/health.py, tests/unit/test_source_health.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(collectors): отслеживание здоровья источников"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
