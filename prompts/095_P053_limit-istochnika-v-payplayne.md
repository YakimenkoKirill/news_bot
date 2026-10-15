# Шаг 095 из 096 · P053 · Этап 15 — Лимит источника в очереди публикации

**Задача трекера:** Лимит источника: подключение
**Результат:** Все пути в очередь публикации проходят через лимит источника: обработка моделью, одобрение и правка на модерации, «Повторить»
**Коммит:** `feat(pipeline): лимит источника при постановке в очередь`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/095_P053_limit-istochnika-v-payplayne.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Сейчас пост встаёт в очередь (переход в approved) в четырёх местах. Каждое должно идти через admit_to_queue из шага 094.

1. **Общий вход** в app/pipeline/queue_limit.py: `@dataclass(frozen=True) QueueAdmission` с полями news_repo и source_repo (фабрики репозиториев от сессии), max_per_source (publisher.max_queued_per_source), decay_per_h (publisher.score_decay_per_h) и методом `async admit(session, news_id, source_id, expected, *, forced=False) -> AdmitResult` — собирает QueueLimitRepos для этой сессии и вызывает admit_to_queue. Так все места используют одну настройку и одни репозитории.
2. **Обработка моделью** (app/pipeline/ai/processor.py, `_finish`): вместо `set_status(news_id, PROCESSED, APPROVED)` — admit с expected=PROCESSED, forced=False. Результат rejected -> процессор возвращает NewsStatus.FILTERED и пишет лог `ai.queue_limit`; admitted / admitted_displaced -> NewsStatus.APPROVED. Передача: AiRepos получает необязательное поле `admit: Callable[[int, int], Awaitable[AdmitResult]] | None = None` (news_id, source_id); None -> прежнее поведение (так существующие тесты процессора не меняются). source_id процессор берёт из новости — если ProcessableNews его не содержит, добавь `source_id: int` в Protocol (NewsItem подходит структурно). В app/pipeline/stage_ai.py, где создаётся AiRepos, передай `admit=partial(admission.admit, session)` с expected=PROCESSED; AiStageDeps получает `admission: QueueAdmission | None = None`.
3. **Модерация** (app/bot/handlers/moderation.py): кнопка «Опубликовать» (pending_review -> approved) и сохранение нового текста (on_new_summary) — admit с expected=PENDING_REVIEW, forced=True (решение админа всегда попадает в очередь; если лимит занят — вытесняется слабейший автоматический пост). Если был displaced_id, к решению в сообщении дописывается «вытеснен слабейший пост источника из очереди». Переход в rejected не меняется. ModerationDeps получает `admission: QueueAdmission | None = None`; None -> прежний set_status.
4. **«Повторить»** (app/pipeline/retry.py): для новости с пересказом (to_queue) — admit с expected=FAILED, forced=True вместо `set_status(FAILED, APPROVED)`; переход в normalized (to_ai) не меняется. RetryDeps получает `admission: QueueAdmission | None = None`.
5. **Публикация вне очереди** (publish_now, pending_review -> approved -> published) лимит не проверяет: пост сразу уходит в канал и в очереди не остаётся. Оставь как есть и добавь об этом строку в docstring publish_now.
6. **app/main.py:** один экземпляр QueueAdmission (NewsRepository, SourceRepository, настройки) передаётся в AiStageDeps, ModerationDeps и RetryDeps (RetryDeps используется внутри ErrorsDeps).

**Критерий готовности:** Ни один путь, кроме publish_now, не ставит пост в очередь мимо лимита; при max_queued_per_source = 0 поведение полностью прежнее.

**Граничные случаи:** Модель одобрила пятый пост источника при лимите 4 и он слабее остальных -> filtered с причиной queue_limit, в очереди те же 4; сильнее -> в очереди, слабейший вытеснен; модератор одобряет при полной очереди -> пост в очереди, вытеснен слабейший; «Повторить» при полной очереди -> так же; StaleStatusError при параллельном решении модераторов обрабатывается как раньше («Уже обработано»); источник с moderation_mode = review -> лимит проверяется при одобрении, а не при обработке моделью.

**Тесты:** `tests/unit/test_ai_processor.py` — admit вызывается с expected=PROCESSED, rejected -> FILTERED, без admit — прежнее поведение; `tests/unit/test_stage_ai.py` — admission передаётся в процессор; `tests/unit/test_bot_moderation.py` — «Опубликовать» и правка текста с forced=True, сообщение о вытеснении; `tests/unit/test_retry.py` — to_queue через admit с forced=True; `tests/integration/test_pipeline.py` или новый `tests/integration/test_queue_limit_pipeline.py` (маркер integration) — при лимите 2 пять одобренных моделью постов одного источника с разными оценками -> в очереди два самых сильных, три в filtered с причиной queue_limit. Старые проверки не ослабляй.

**Контекст проекта:** Python 3.11+, aiogram 3.x. Отображение в /queue, /status, отчёте и очистка при старте — шаг 096.

**Файлы:** app/pipeline/queue_limit.py, app/pipeline/ai/processor.py, app/pipeline/stage_ai.py, app/bot/handlers/moderation.py, app/bot/texts.py, app/pipeline/retry.py, app/publisher/stage_publish.py (только docstring), app/main.py, тестовые файлы из списка выше.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Смена статуса — только через set_status и app/domain/status.py. Новые зависимости, ключи конфигурации, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй.

**Формат ответа:** Полные файлы из списка «Файлы». Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(pipeline): лимит источника при постановке в очередь"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
