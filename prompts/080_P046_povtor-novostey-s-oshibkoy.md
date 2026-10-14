# Шаг 080 из 082 · P046 · Этап 12 — Журнал ошибок в боте управления

**Задача трекера:** Повтор новостей, упавших с ошибкой
**Результат:** Список новостей в статусе failed и функция retry_failed, которая возвращает новость в нужную стадию
**Коммит:** `feat(pipeline): повтор новостей со статусом failed`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/080_P046_povtor-novostey-s-oshibkoy.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Новость получает failed в двух случаях: модель вернула некорректный ответ (normalized -> failed, reason bad_ai_response, пересказа нет) или не удалась публикация (approved/scheduled -> failed, reason publish_error или no_processed_post, пересказ есть). Сейчас вернуть её можно только правкой БД. Дай для этого функцию, которую вызовет кнопка в /errors (шаг 081).

1. **Машина состояний** (app/domain/status.py): добавь переход `FAILED -> NORMALIZED` (повторная обработка моделью). Переход `FAILED -> APPROVED` уже есть.
2. **NewsRepository** (app/db/repositories/news.py):
   - `list_failed(limit: int, offset: int) -> list[tuple[NewsItem, bool]]` — новости в failed и признак «есть пересказ» (LEFT JOIN processed_posts), сначала новые (id DESC);
   - `count_failed() -> int`.
3. **Функция** `retry_failed(news_id: int, deps: RetryDeps) -> RetryResult` в app/pipeline/retry.py:
   - `RetryDeps`: session_scope, news_repo, post_repo (Protocol с нужными методами — get и get_by_news);
   - `RetryOutcome(StrEnum)`: `to_ai` (вернули в normalized — модель обработает заново), `to_queue` (вернули в approved — встанет в очередь публикации по приоритету), `not_failed` (статус уже другой), `not_found`;
   - `RetryResult`: outcome и текущий статус;
   - в одной транзакции: новость не найдена -> not_found; статус не failed -> not_failed; пересказ есть -> `set_status(FAILED, APPROVED)`, иначе -> `set_status(FAILED, NORMALIZED)`; StaleStatusError (параллельный повтор) -> not_failed с перечитанным статусом;
   - лог `retry.failed_news` с news_id и outcome.
   - reject_reason не очищай: при повторной ошибке он перезапишется, а по истории видно прошлую причину.

**Критерий готовности:** Новость с ошибкой модели возвращается в normalized и снова уходит в ИИ-стадию; новость с ошибкой публикации возвращается в approved и снова публикуется; повтор не нарушает машину состояний.

**Граничные случаи:** Двойное нажатие «Повторить» -> второй вызов not_failed; новость уже published -> not_failed; failed без пересказа -> to_ai; failed с пересказом -> to_queue; повтор новости с ошибкой модели тратит дневной бюджет как обычная обработка — отдельного кода не нужно, проверь, что ИИ-стадия её берёт; list_failed — limit <= 0 или offset < 0 -> ValueError.

**Тесты:** `tests/unit/test_status.py` — дописать разрешённый переход failed -> normalized (остальные проверки таблицы переходов не ослабляй, обнови только ожидаемое множество для failed); `tests/unit/test_retry.py` — все исходы retry_failed на fakes; `tests/integration/test_repo_news_failed.py` (маркер integration) — list_failed и count_failed, признак пересказа, порядок, повтор с реальной БД и последующий захват claim_batch(NORMALIZED) или claim_next.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async), PostgreSQL 16. Кнопка «🔁 Повторить» — шаг 081.

**Файлы:** app/domain/status.py, app/db/repositories/news.py, app/pipeline/retry.py, tests/unit/test_status.py, tests/unit/test_retry.py, tests/integration/test_repo_news_failed.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Смена статуса — только через set_status и app/domain/status.py. Других переходов, кроме failed -> normalized, не добавляй. Новые зависимости, ключи конфигурации, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/domain/status.py, app/db/repositories/news.py, app/pipeline/retry.py, tests/unit/test_status.py, tests/unit/test_retry.py, tests/integration/test_repo_news_failed.py. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(pipeline): повтор новостей со статусом failed"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
