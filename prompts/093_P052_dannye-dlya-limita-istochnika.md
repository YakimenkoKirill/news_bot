# Шаг 093 из 096 · P052 · Этап 15 — Лимит источника в очереди публикации

**Задача трекера:** Лимит постов одного источника в очереди
**Результат:** Переходы для вытеснения из очереди, блокировка источника и выборки очереди по источнику
**Коммит:** `feat(db): данные для лимита источника в очереди`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/093_P052_dannye-dlya-limita-istochnika.md». После коммита: `python prompts/next_step.py --done`.

## Задача

В очереди публикации (статусы approved и scheduled) одновременно должно быть не больше `publisher.max_queued_per_source` постов одного источника. Лишний пост вытесняется в filtered с причиной `queue_limit`. Подготовь для этого машину состояний и запросы.

1. **Машина состояний** (app/domain/status.py): добавь переходы `APPROVED -> FILTERED` и `SCHEDULED -> FILTERED` (вытеснение из очереди). Другие переходы не меняй.
2. **SourceRepository** (app/db/repositories/sources.py): `lock(source_id) -> bool` — `SELECT id FROM sources WHERE id = :id FOR UPDATE`; False, если источника нет. Нужен, чтобы две новости одного источника не прошли лимит одновременно.
3. **NewsRepository** (app/db/repositories/news.py), используя существующий `_priority(decay_per_h)`:
   - `queued_by_source(source_id, statuses, decay_per_h) -> list[QueuedRef]` — `QueuedRef` (frozen dataclass: news_id, status, priority) для постов источника в этих статусах с пересказом, от самого слабого к самому сильному (priority ASC, id DESC);
   - `priority_of(news_id, decay_per_h) -> float | None` — приоритет одной новости (None — нет пересказа или новости);
   - `queue_counts_by_source(statuses) -> dict[int, int]` — сколько постов в очереди у каждого источника (только с пересказом).
4. **Конфигурация:** `publisher.max_queued_per_source: 4` (int, ge=0; 0 — без лимита) в configs/base.yaml и PublisherConfig.

**Критерий готовности:** Переходы разрешены и проверены; lock блокирует строку до конца транзакции; приоритеты из queued_by_source и priority_of совпадают с порядком list_prepared.

**Граничные случаи:** lock несуществующего источника -> False; вторая сессия на lock того же источника ждёт, пока первая не завершит транзакцию (проверь с таймаутом через asyncio.wait_for); queued_by_source без постов -> []; посты в pending_review не попадают при статусах (approved, scheduled); равные приоритеты -> слабее пост с большим id: это ровно обратный порядок list_prepared (priority DESC, id ASC), где при равенстве раньше выходит меньший id; priority_of для новости без пересказа -> None.

**Тесты:** `tests/unit/test_status.py` — новые переходы (обнови только ожидаемые множества для approved и scheduled); `tests/integration/test_repo_queue_limit.py` (маркер integration) — lock (в том числе ожидание второй сессии), queued_by_source (порядок, статусы, равные приоритеты), priority_of, queue_counts_by_source; `tests/unit/test_config.py` — новый ключ.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async), PostgreSQL 16. Схема БД не меняется. Логика лимита — шаг 094.

**Файлы:** app/domain/status.py, app/db/repositories/sources.py, app/db/repositories/news.py, configs/base.yaml, app/config.py, tests/unit/test_status.py, tests/integration/test_repo_queue_limit.py, tests/unit/test_config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Смена статуса — только через set_status и app/domain/status.py. Новые зависимости, миграции, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: publisher.max_queued_per_source = 4.

**Формат ответа:** Полные файлы из списка «Файлы». Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): данные для лимита источника в очереди"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
