# Шаг 105 из 113 · P057 · Этап 17 — Производительность БД

**Задача трекера:** Производительность БД
**Результат:** Тексты старых новостей в конечных статусах очищаются по расписанию
**Коммит:** `feat(db): очистка текстов старых новостей`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/105_P057_ochistka-staryh-tekstov-novostey.md». После коммита: `python prompts/next_step.py --done`.

## Задача

raw_text и clean_text хранятся вечно для всех новостей, включая отсеянные и дубли — это основная часть объёма БД.

1. **Репозиторий:** `NewsRepository.strip_texts(before: datetime, limit: int) -> int` — для новостей в конечных статусах (TERMINAL_STATUSES из app/domain/status.py: published, filtered, duplicate, rejected) с created_at < before и ещё не очищенным текстом: raw_text = '', clean_text = NULL. Не трогать: заголовок, url, хеши, simhash и его части, cluster_id, raw_meta (нужны для дублей, отчётов и картинок). Обработка пачками по limit (`UPDATE ... WHERE id IN (SELECT id ... LIMIT :limit FOR UPDATE SKIP LOCKED)`).
2. **Задача** `retention` в планировщике раз в scheduler.retention_every_h: вызывает strip_texts пачками, пока возвращается limit, но не дольше retention.max_run_s за запуск; лог `retention.stripped` с количеством.
3. **Конфигурация:** секция `retention`: `text_days: 30` (ge = dedup.window_h / 24 + 1 — проверь валидатором, чтобы не очистить тексты, нужные поиску дублей), `batch_size: 1000`, `max_run_s: 60`; `scheduler.retention_every_h: 24`.
4. **README.md:** что очищается, что остаётся, как изменить срок.

**Критерий готовности:** Через text_days дней у новостей в конечных статусах нет текста, а дубли, отчёты, /queue и /errors работают как раньше.

**Граничные случаи:** Новости в normalized, approved, scheduled, pending_review, failed не очищаются никогда; уже очищенные не обновляются повторно; text_days меньше окна дублей -> ошибка конфигурации; большой объём -> задача останавливается по max_run_s и продолжает в следующий запуск; параллельная нормализация не блокируется (SKIP LOCKED).

**Тесты:** `tests/integration/test_repo_retention.py` — статусы, граница даты, повторный запуск, пачки; `tests/unit/test_scheduler.py` — задача и интервал; `tests/unit/test_config.py` — секция и валидатор.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async), APScheduler, PostgreSQL 16.

**Файлы:** app/db/repositories/news.py, app/scheduler.py, app/main.py, app/config.py, configs/base.yaml, README.md, тестовые файлы из списка выше.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). Смена статуса новости — только через set_status и app/domain/status.py. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Если меняется состав или порядок компонентов в app/main.py — обнови ожидания tests/unit/test_app_wiring.py; новая секция конфигурации должна быть описана в README.md (это проверяет tests/unit/test_readme.py). Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: retention.text_days = 30, retention.batch_size = 1000, retention.max_run_s = 60, scheduler.retention_every_h = 24.

**Формат ответа:** Полные файлы из списка «Файлы»; для README.md — новый или изменённый раздел целиком. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): очистка текстов старых новостей"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
