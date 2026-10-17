# Шаг 116 из 117 · P066 · Этап 21 — Порядок очереди без учёта источника

**Задача трекера:** Порядок очереди без учёта источника
**Результат:** Параметр publisher.source_aware: при false очередь упорядочена только по оценке интереса с поправкой на свежесть, лимит источника и бонус за число источников не действуют
**Коммит:** `feat(queue): порядок очереди без учёта источника`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/116_P066_rezhim-ocheredi-bez-ucheta-istochnika.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Сейчас на очередь влияют два фактора, связанные с источником: бонус за число источников события (publisher.cluster_bonus, шаг 106) и лимит постов одного источника (publisher.max_queued_per_source, шаги 093–096). Нужен режим, в котором порядок определяется только актуальностью новости: `оценка интереса − score_decay_per_h × возраст в часах`.

1. **Конфигурация:** `publisher.source_aware: false` (bool) в PublisherConfig и configs/base.yaml с комментарием: «true — учитывать источник (бонус за число источников и лимит источника), false — только оценка интереса и свежесть». Значения cluster_bonus, cluster_bonus_max и max_queued_per_source в yaml не меняются — они снова начнут действовать при source_aware = true.
2. **Одно место решения** (app/db/repositories/news.py, рядом с Priority):
   - `Priority.from_config(cfg)` при `source_aware = false` возвращает cluster_bonus = 0 и cluster_bonus_max = 0;
   - функция `effective_max_queued(cfg) -> int`: 0 при source_aware = false, иначе cfg.max_queued_per_source. Если cfg — Protocol PriorityWeights, добавь в него source_aware.
3. **Все чтения лимита источника** идут через effective_max_queued, а не напрямую через max_queued_per_source: QueueAdmission и его limits() (app/pipeline/queue_limit.py), сборка в app/main.py (QueueAdmission, QueueTrimComponent), /queue (app/bot/handlers/queue.py — заполненность «N/max»), /status (app/bot/handlers/status.py — строка о заполненных источниках). Найди их поиском по `max_queued_per_source` и `Priority.from_config` — после шага не должно остаться прямых чтений max_queued_per_source вне app/config.py, runtime_config.py и effective_max_queued.
4. **Изменяемый на лету параметр** (app/runtime_config.py): RuntimeKey `publisher.source_aware` с парсером `_parse_bool` (принимает true/false, 1/0, да/нет, вкл/выкл без учёта регистра; иначе ValueError с подсказкой). При смене source_aware на true сразу вызывается вытеснение лишнего, как сейчас при смене лимита: в app/runtime_config.py рядом с QUEUE_LIMIT_KEY объяви `TRIM_KEYS = frozenset({QUEUE_LIMIT_KEY, "publisher.source_aware"})`, а в `on_runtime_change` (app/main.py) проверяй `key in TRIM_KEYS` вместо сравнения с одним QUEUE_LIMIT_KEY.
5. **Ничего не удаляется:** при source_aware = false посты, вытесненные раньше по лимиту, не возвращаются; cluster_sources продолжает считаться в нормализации (пригодится при обратном включении); пометки и отчёт по лимиту — шаг 117.

**Критерий готовности:** При source_aware = false из двух постов выходит тот, у кого выше «оценка − старение», независимо от источника и числа источников события, и в очередь попадают все посты, прошедшие порог; при true — поведение как до шага.

**Граничные случаи:** source_aware = false и cluster_sources = 5 -> бонуса нет; source_aware = false и 10 постов одного источника -> все в очереди; переключение false -> true -> лишние сразу вытесняются по лимиту; true -> false -> ничего не вытесняется и не возвращается; значение «вкл»/«нет»/«1» в /settings разбирается, «может быть» -> ошибка с подсказкой; publish_due, /queue и ETA используют один и тот же Priority (порядок совпадает).

**Тесты:** `tests/unit/test_queue_limit.py` — Priority.from_config и effective_max_queued в обоих режимах, admit при source_aware = false допускает сверх лимита; `tests/unit/test_runtime_config.py` — _parse_bool и ключ; `tests/integration/test_queue_order_modes.py` (маркер integration) — порядок list_prepared и claim_next в обоих режимах на одних данных, вытеснение при переключении на true; `tests/unit/test_config.py` — новый ключ.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async), pydantic 2. Отображение режима в /queue, /status, /settings и README — шаг 117.

**Файлы:** app/db/repositories/news.py, app/pipeline/queue_limit.py, app/bot/handlers/queue.py, app/bot/handlers/status.py, app/runtime_config.py, app/main.py, app/config.py, configs/base.yaml, тестовые файлы из списка выше.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). Смена статуса новости — только через set_status и app/domain/status.py. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Если меняется состав или порядок компонентов в app/main.py — обнови ожидания tests/unit/test_app_wiring.py; новая секция конфигурации должна быть описана в README.md (это проверяет tests/unit/test_readme.py). Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: publisher.source_aware = false.

**Формат ответа:** Полные файлы из списка «Файлы». Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(queue): порядок очереди без учёта источника"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
