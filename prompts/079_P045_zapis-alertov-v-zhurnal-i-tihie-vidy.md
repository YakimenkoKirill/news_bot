# Шаг 079 из 082 · P045 · Этап 12 — Журнал ошибок в боте управления

**Задача трекера:** Журнал ошибок: хранение событий
**Результат:** AlertService записывает каждое событие в журнал, а в чат присылает только «громкие» виды; ошибки модели по отдельной новости тоже попадают в журнал
**Коммит:** `feat(alerts): запись событий в журнал и тихие виды ошибок`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/079_P045_zapis-alertov-v-zhurnal-i-tihie-vidy.md». После коммита: `python prompts/next_step.py --done`.

## Задача

1. **Хранилище для AlertService** (app/alerts.py):
   - Protocol `IncidentStore` с методом `record(event: AlertEvent) -> RecordedIncident | None`, где RecordedIncident — Protocol с полями id и count (Incident подходит структурно).
   - Protocol `MutedKinds`: `async () -> set[AlertKind]` — какие виды не присылать в чат (реализацию из настроек подключит шаг 082).
   - Новые keyword-only параметры конструктора: `store: IncidentStore | None = None`, `muted: MutedKinds | None = None`, `silent_kinds: frozenset[AlertKind] = frozenset()`. Значения по умолчанию сохраняют текущее поведение, поэтому app/main.py в этом шаге не меняется.
2. **Порядок в emit:**
   1. Записать событие в store — всегда, в том числе если повтор потом будет подавлен throttle. Ошибка записи (например, недоступна БД) -> `log.exception("alert.store_failed")`, и отправка в чат продолжается: если упала БД, админы всё равно должны узнать.
   2. Если вид в `silent_kinds` или в `await muted()` -> не присылать в чат, лог `alert.silent`. Ошибка при чтении muted -> лог и считать, что заглушённых нет.
   3. Throttle и отправка — как сейчас. К тексту добавить строку «Повторов: {count} · /errors», если count > 1, иначе «Подробнее: /errors».
3. **Новый вид** `AlertKind.AI_ITEM_FAILED = "ai_item_failed"` в app/domain/alerts.py и шаблон в texts.ALERT_TEMPLATES: «🟠 <b>Модель не смогла обработать новость</b>\n{message}».
4. **Где он возникает** (app/pipeline/stage_ai.py): когда processor вернул NewsStatus.FAILED (bad_ai_response) и когда обработка новости упала исключением (ветка `ai_stage.item_failed`) — `sink.emit` с key `ai_item_failed:{news_id}` и текстом, где есть заголовок новости и причина или `repr` ошибки. Ошибка emit не должна ломать пачку (лог и продолжение). Алерт ai_down для всей модели остаётся как есть.
5. **Конфигурация** (configs/base.yaml и AlertsConfig в app/config.py):
   - `alerts.silent_kinds: ["ai_item_failed"]` — виды, которые только пишутся в журнал (список AlertKind; неизвестное значение -> ошибка при запуске);
   - `alerts.max_message_chars: 2000` — сколько символов сообщения хранить в журнале.

**Критерий готовности:** Каждое событие, включая подавленные повторы и тихие виды, попадает в store; в чат приходят только незаглушённые виды с подсказкой про /errors; без store поведение прежнее.

**Граничные случаи:** Повтор внутри throttle_min -> запись есть, сообщения нет; store бросает исключение -> сообщение в чат всё равно уходит; тихий вид -> записан, не отправлен, throttle не трогается; muted бросает исключение -> отправка как обычно; count = 1 и count > 1 — разные подписи; ai_item_failed при ошибке emit не останавливает остальные новости пачки.

**Тесты:** `tests/unit/test_alerts.py` — дописать все граничные случаи выше (fake store и muted); старые проверки не ослабляй, допустимо только учесть новую подпись в тексте. `tests/unit/test_stage_ai.py` — emit ai_item_failed для FAILED и для исключения, ошибка emit не ломает пачку. `tests/unit/test_config.py` — новые ключи читаются, неизвестный вид в silent_kinds отклоняется.

**Контекст проекта:** Python 3.11+, aiogram 3.x. Реальный store поверх IncidentRepository (шаг 078) и чтение заглушённых видов из настроек подключаются в app/main.py на шаге 082.

**Файлы:** app/alerts.py, app/domain/alerts.py, app/bot/texts.py, app/pipeline/stage_ai.py, configs/base.yaml, app/config.py, tests/unit/test_alerts.py, tests/unit/test_stage_ai.py, tests/unit/test_config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Секреты не попадают ни в журнал, ни в лог. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: alerts.silent_kinds = ["ai_item_failed"], alerts.max_message_chars = 2000.

**Формат ответа:** Полные файлы: app/alerts.py, app/domain/alerts.py, app/bot/texts.py, app/pipeline/stage_ai.py, configs/base.yaml, app/config.py, tests/unit/test_alerts.py, tests/unit/test_stage_ai.py, tests/unit/test_config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(alerts): запись событий в журнал и тихие виды ошибок"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
