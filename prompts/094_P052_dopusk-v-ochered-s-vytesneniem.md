# Шаг 094 из 096 · P052 · Этап 15 — Лимит источника в очереди публикации

**Задача трекера:** Лимит постов одного источника в очереди
**Результат:** app/pipeline/queue_limit.py — admit_to_queue (допуск в очередь с вытеснением по приоритету) и trim_queue
**Коммит:** `feat(pipeline): лимит постов одного источника в очереди`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/094_P052_dopusk-v-ochered-s-vytesneniem.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Правило: в очереди (approved и scheduled) не больше `max_queued_per_source` постов одного источника. Если лимит занят, остаются самые сильные по приоритету (оценка интереса с поправкой на свежесть — та же формула, что у порядка очереди).

1. **Причина:** `REASON_QUEUE_LIMIT = "queue_limit"` объяви в app/pipeline/queue_limit.py — в проекте причины отказа живут в модуле, который их ставит (как REASON_NOT_RELEVANT в processor.py, REASON_TOO_OLD в filters.py).
2. **Зависимости:** `QueueLimitRepos` (frozen dataclass) с news (Protocol: set_status, queued_by_source, priority_of, queue_counts_by_source из шага 093) и sources (Protocol: lock). `QUEUE_STATUSES = (NewsStatus.APPROVED, NewsStatus.SCHEDULED)`.
3. **`AdmitOutcome(StrEnum)`**: `admitted` (встала в очередь), `admitted_displaced` (встала, вытеснив слабейший пост источника), `rejected` (сама слабее всех — в filtered). **`AdmitResult`**: outcome, status (итоговый статус новости), displaced_id (int | None).
4. **`admit_to_queue(news_id, source_id, expected: NewsStatus, repos, max_per_source, decay_per_h, *, forced=False) -> AdmitResult`** — вызывается внутри уже открытой транзакции вызывающего:
   1. max_per_source = 0 -> просто `set_status(expected -> APPROVED)`, admitted.
   2. `sources.lock(source_id)` — до конца транзакции никто другой не допускает посты этого источника.
   3. `queued = queued_by_source(source_id, QUEUE_STATUSES, decay)` (самый слабый первым). Если len < max -> `expected -> APPROVED`, admitted.
   4. Иначе `mine = priority_of(news_id, decay)`; `weakest = queued[0]` (QueuedRef: news_id, status, priority).
      - `forced` (решение админа: одобрение на модерации, правка текста, «Повторить») или `mine > weakest.priority` -> `set_status(weakest.news_id, weakest.status, FILTERED, reason=queue_limit)`, новость `expected -> APPROVED`, admitted_displaced.
      - иначе -> новость `expected -> FILTERED` с reason `queue_limit`, rejected. Для expected = PROCESSED это разрешённый переход; для других expected без forced — InvalidTransition, поэтому не-forced вызовы допустимы только из PROCESSED (проверь аргументы и брось ValueError заранее).
   5. Лог `queue_limit.admit` с news_id, source_id, outcome, displaced_id, mine и weakest.priority.
   - Если очередь источника уже переполнена сильнее, чем на один пост (лимит уменьшили), admit вытесняет ровно один пост; остальное лишнее убирает trim_queue.
5. **`trim_queue(repos, max_per_source, decay_per_h) -> int`** — для каждого источника из queue_counts_by_source с количеством больше лимита: lock, затем вытесняет самых слабых (`-> FILTERED`, reason `queue_limit`), пока не останется max. Возвращает число вытесненных. max = 0 -> 0, ничего не делает.

**Критерий готовности:** При любой последовательности допусков в очереди у источника не больше max постов, и это самые сильные по приоритету; админские решения всегда попадают в очередь.

**Граничные случаи:** Очередь источника пуста -> admitted; ровно max - 1 -> admitted; max и новый сильнее -> admitted_displaced, вытеснен слабейший; max и новый слабее или равен -> rejected; forced при полном лимите -> admitted_displaced даже если новый слабее; слабейший в статусе scheduled -> scheduled -> filtered; max = 0 -> лимита нет; trim_queue при превышении на 3 -> вытеснено 3 самых слабых; не-forced вызов с expected не PROCESSED -> ValueError; приоритет новости None (нет пересказа) -> ValueError.

**Тесты:** `tests/unit/test_queue_limit.py` — все граничные случаи на fakes (репозитории в памяти, приоритет задаётся в тесте); `tests/integration/test_queue_limit_db.py` (маркер integration) — два параллельных admit одного источника при max = 1 с реальной БД (две сессии, asyncio.gather) -> в очереди ровно один пост, сильнейший; trim_queue на реальных данных.

**Контекст проекта:** Python 3.11+. Вызовы из обработки моделью, модерации и «Повторить» — шаг 095; trim_queue при старте приложения — шаг 096.

**Файлы:** app/pipeline/queue_limit.py, tests/unit/test_queue_limit.py, tests/integration/test_queue_limit_db.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Смена статуса — только через set_status и app/domain/status.py. Новые зависимости, ключи конфигурации, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй.

**Формат ответа:** Полные файлы из списка «Файлы». Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(pipeline): лимит постов одного источника в очереди"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
