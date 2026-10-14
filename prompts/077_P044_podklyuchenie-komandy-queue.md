# Шаг 077 из 077 · P044 · Этап 11 — Очередь публикации в боте управления

**Задача трекера:** Подключение очереди к приложению и документация
**Результат:** /queue подключена в app/main.py, есть в меню и README, сквозной тест с PostgreSQL
**Коммит:** `feat(app): команда /queue в боте управления`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/077_P044_podklyuchenie-komandy-queue.md». После коммита: `python prompts/next_step.py --done`.

## Задача

1. **app/main.py:** собери `ManualPublishDeps(publish=publish_deps, news_repo=NewsRepository)` и `PreviewDeps` (те же session_scope, репозитории, image_check и settings.publisher, что у notifier). Добавь `queue_handlers.create_router()` в список routers (до fallback — create_dispatcher подключает его последним сам) и `"queue_deps": QueueDeps(...)` в workflow_data: page_size = settings.bot.page_size, title_chars = settings.bot.queue_title_chars, channel_id = settings.telegram_channel_id, tz = tz.
2. **app/bot/texts.py:** добавь в COMMANDS сразу после "status" команду `"queue": "подготовленные посты, публикация вне очереди"`, чтобы она попала в /help, в меню команд и в клавиатуру /start.
3. **README.md:** в разделе о боте управления опиши приоритет очереди и /queue: посты выходят по приоритету «оценка интереса − publisher.score_decay_per_h × возраст в часах», а не по времени сбора; как выбрать коэффициент (0 — только по оценке, больше — сильнее важна свежесть); две вкладки, порядок равен порядку выхода, у каждого поста видна оценка, время «≈» — оценка, «Опубликовать сейчас» игнорирует интервал, тихие часы и паузу, но не лимиты длины и не проверку картинки; после ручного поста обычная очередь ждёт publisher.min_interval_min.
4. **Сквозной тест** с реальной БД — tests/integration/test_queue_publish.py (маркер integration; Publisher заменён fake, как в tests/integration/test_pipeline.py):
   - три approved-поста с разными оценками и возрастом, слот открыт: три вызова publish_due с шагом min_interval_min публикуют их в порядке приоритета, и этот порядок совпадает с list_prepared до начала публикации;
   - два approved-поста и публикация минуту назад: publish_due -> skipped="slot"; publish_now для поста с меньшим приоритетом -> published, Publication(sent), статус published, пост с большим приоритетом остаётся approved;
   - сразу после этого publish_due -> skipped="slot" с next_slot = now + min_interval_min;
   - пока параллельная сессия держит claim_batch на посте, publish_now для него -> busy, и publisher.send не вызывается;
   - pending_review -> publish_now -> published;
   - list_prepared для вкладки queue после этого не содержит опубликованных постов.

**Критерий готовности:** После запуска `python -m app` команда /queue есть в меню бота, показывает очередь, а «Опубликовать сейчас» публикует пост в канал; make lint, make test и make test-integration проходят.

**Граничные случаи:** Порядок роутеров: /queue не перехватывается fallback-подсказкой «неизвестная команда»; middleware доступа только для админов распространяется на новый роутер (проверь тестом, что не-админ получает отказ на /queue и на callback q:...); tests/unit/test_bot_common.py с проверками /help и меню проходит (новая команда появляется в них автоматически).

**Тесты:** `tests/integration/test_queue_publish.py` (сценарии выше); `tests/unit/test_bot_middleware.py` — дописать проверку отказа не-админу для /queue и callback очереди. Отдельного теста сборки app/main.py в проекте нет; правильность подключения проверь запуском `python -c "import app.main"` и прогоном mypy.

**Контекст проекта:** Python 3.11+, aiogram 3.x, PostgreSQL 16. Последний шаг этапа 11.

**Файлы:** app/main.py, app/bot/texts.py, README.md, tests/integration/test_queue_publish.py, tests/unit/test_bot_middleware.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Новые зависимости, ключи конфигурации, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Раздел «Автопилот» в CLAUDE.md и файлы prompts/ не меняй.

**Формат ответа:** Полные файлы: app/main.py, app/bot/texts.py, tests/integration/test_queue_publish.py, tests/unit/test_bot_middleware.py; для README.md — новый раздел целиком. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(app): команда /queue в боте управления"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
