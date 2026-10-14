# Шаг 074 из 077 · P043 · Этап 11 — Очередь публикации в боте управления

**Задача трекера:** Очередь подготовленных постов в боте управления
**Результат:** app/bot/preview.py — общий предпросмотр поста для модерации и очереди
**Коммит:** `refactor(bot): общий предпросмотр поста`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/074_P043_obschiy-predprosmotr-posta.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Предпросмотр поста «ровно как в канале» сейчас живёт внутри BotReviewNotifier (app/bot/moderation.py), а нужен ещё и команде /queue. Вынеси его в отдельный модуль без изменения поведения.

1. Создай app/bot/preview.py:
   - `@dataclass(frozen=True) PostPreview`: fitted (FittedPost), image_url (str | None), link (str) — бывший `_Preview`.
   - `@dataclass(frozen=True) PreviewDeps`: session_scope, news_repo, source_repo, check_image, cfg (PublisherConfig) с теми же Protocol, что сейчас у ReviewNotifierDeps (ReviewNewsRepo, ReviewSourceRepo перенеси сюда; в moderation.py оставь их импорт, чтобы старые импорты работали).
   - `async def build_preview(post: ProcessedPost, deps: PreviewDeps) -> PostPreview | None` — бывший `_build`: тот же build_post и fit_post, что при публикации; нет новости или источника -> лог review.missing_data и None.
   - `async def send_preview(bot, chat_id: int, preview: PostPreview, keyboard: InlineKeyboardMarkup | None) -> Any` — бывший `_send_preview`: фото с подписью, при TelegramBadRequest — текстом с LinkPreviewOptions. Возвращает отправленное сообщение.
   - `async def mark_message(callback, decision: str, who: str) -> None` — перенеси `_mark_message` из app/bot/handlers/moderation.py: убирает кнопки и дописывает решение к сообщению (подпись или текст), при ошибке редактирования — отдельное сообщение.
2. BotReviewNotifier и handlers/moderation.py используют функции из preview.py. ReviewNotifierDeps и ModerationDeps не меняются: notifier собирает PreviewDeps из своих полей.

**Критерий готовности:** Модерация работает как раньше: tests/unit/test_bot_review_notify.py и tests/unit/test_bot_moderation.py проходят без изменений; preview.py покрыт своими тестами.

**Граничные случаи:** Картинка отклонена Telegram -> предпросмотр текстом; у поста нет картинки -> сразу текст; нет источника -> None без исключения; keyboard=None -> сообщение без кнопок; mark_message для фото-сообщения редактирует подпись, для текстового — текст.

**Тесты** (`tests/unit/test_bot_preview.py`): build_preview (с картинкой, без картинки, нет источника), send_preview (фото, откат на текст при TelegramBadRequest, без клавиатуры), mark_message (подпись, текст, откат при ошибке редактирования). Fakes бота — по образцу tests/unit/test_bot_review_notify.py.

**Контекст проекта:** Python 3.11+, aiogram 3.x. Это чистый рефакторинг перед шагом 075 (/queue).

**Файлы:** app/bot/preview.py, app/bot/moderation.py, app/bot/handlers/moderation.py, tests/unit/test_bot_preview.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Поведение модерации, тексты и клавиатуры не меняй. Существующие тесты не меняй и не ослабляй. Новые зависимости, ключи конфигурации, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/bot/preview.py, app/bot/moderation.py, app/bot/handlers/moderation.py, tests/unit/test_bot_preview.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "refactor(bot): общий предпросмотр поста"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
