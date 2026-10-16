# Шаг 101 из 113 · P056 · Этап 16 — Надёжность и расходы на модель

**Задача трекера:** Расходы на модель
**Результат:** Таблица ai_requests: каждый запрос к модели с токенами, кэшем и стоимостью; бюджет и отчёты считают по ней
**Коммит:** `feat(ai): учёт каждого запроса к модели и попаданий в кэш`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/101_P056_uchet-zaprosov-k-modeli.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Сейчас стоимость хранится только в processed_posts.cost. Если модель дважды вернула плохой ответ, пересказа нет — и стоимость этих запросов не попадает ни в бюджет, ни в отчёт. Также не видно, сколько входных токенов оплачено по цене кэша.

1. **Модель** `AiRequest`, таблица `ai_requests` (миграция 0005, down_revision "0004"): id, news_item_id (FK news_items, ON DELETE SET NULL, NULL допустим), kind (String(16): `summary`; шаг 102 добавит `screen`), model (String(64)), prompt_tokens, cached_tokens, completion_tokens (int), cost (Numeric той же точности, что processed_posts.cost), ok (bool — ответ разобран), created_at (timezone-aware, server_default now()); индекс по created_at.
   - Миграция переносит историю: для каждого processed_post — строка kind = summary, ok = true, с его токенами, cost и created_at; cached_tokens = 0.
2. **Кэш:** AiRawResponse получает `cached_tokens`. Читай `usage.prompt_cache_hit_tokens` (DeepSeek), иначе `usage.prompt_tokens_details.cached_tokens` (OpenAI-формат), иначе 0.
3. **Цена:** `budget.cost(prompt_tokens, cached_tokens, completion_tokens, prices)`: некэшированные входные — price_input_per_1m, кэшированные — `ai.price_input_cached_per_1m`. Этот ключ необязательный (`float | None = None`): если он не задан, кэшированные токены считаются по price_input_per_1m. В configs/base.yaml добавь его закомментированным с пометкой «задаёт человек: цена кэшированного входа у провайдера» — значение не выдумывай.
4. **Запись:** AiProcessor пишет AiRequest на **каждую** попытку (и удачную, и неудачную) через новый репозиторий `AiRequestRepository.add` в той же транзакции, что и итоговый статус. processed_posts.cost остаётся = сумма запросов этой новости (для совместимости).
5. **Бюджет:** BudgetGuard и `cost_since` считают сумму по ai_requests.created_at, а не по processed_posts.
6. **Отчёты:** app/reports/metrics.py (`cost_by_day`) и app/reports/daily.py (расход, запросы, токены) — по ai_requests. В блоке «Модель» ежедневного отчёта — строка «из кэша {P}% входных токенов» и «неудачных запросов {N}».

**Критерий готовности:** Сумма в ai_requests за сутки равна реально оплаченному: включая неудачные попытки и с учётом кэша; бюджет и отчёты считают одинаково.

**Граничные случаи:** Новость упала с bad_ai_response дважды -> два AiRequest(ok=false), бюджет их видит; провайдер не прислал usage -> строка с нулями и лог warning; price_input_cached_per_1m не задан -> цена кэша = обычной; удаление новости -> строки ai_requests остаются с news_item_id = NULL; история до миграции видна в отчёте за прошлые дни.

**Тесты:** `tests/unit/test_ai_client.py` — разбор cached_tokens в обоих форматах; `tests/unit/test_budget.py` — цена с кэшем и без ключа; `tests/unit/test_ai_processor.py` — AiRequest на каждую попытку; `tests/integration/test_repo_ai_requests.py` — репозиторий, cost_since, перенос истории миграцией; `tests/integration/test_migrations.py` — 0005 вверх и вниз; `tests/integration/test_daily_report.py` и `tests/integration/test_run_report.py` — расход по ai_requests (обнови фикстуры: добавь строки ai_requests, ожидания по сумме не меняются).

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async), Alembic, PostgreSQL 16. Отбор по теме коротким запросом — шаг 102.

**Файлы:** app/db/models.py, migrations/versions/0005_ai_requests.py, app/db/repositories/ai_requests.py, app/db/repositories/__init__.py, app/pipeline/ai/client.py, app/pipeline/ai/budget.py, app/pipeline/ai/processor.py, app/pipeline/stage_ai.py, app/main.py, app/reports/metrics.py, app/reports/daily.py, app/reports/daily_format.py, app/config.py, configs/base.yaml, тестовые файлы из списка выше.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). Смена статуса новости — только через set_status и app/domain/status.py. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Если меняется состав или порядок компонентов в app/main.py — обнови ожидания tests/unit/test_app_wiring.py; новая секция конфигурации должна быть описана в README.md (это проверяет tests/unit/test_readme.py). Новый ключ ai.price_input_cached_per_1m — необязательный: добавь поле `float | None = None` в AiConfig и закомментированную строку в configs/base.yaml с пометкой «задаёт человек»; отсутствие значения — не пробел и не причина останавливаться: тогда кэш считается по price_input_per_1m.

**Формат ответа:** Полные файлы из списка «Файлы». Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(ai): учёт каждого запроса к модели и попаданий в кэш"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
