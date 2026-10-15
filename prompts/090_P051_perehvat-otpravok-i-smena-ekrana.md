# Шаг 090 из 096 · P051 · Этап 14 — Один экран в чате бота

**Задача трекера:** Один экран: хранение и учёт сообщений
**Результат:** app/bot/screen.py — номер экрана в ContextVar, ScreenTracker и middleware запросов бота, который удаляет прошлый экран
**Коммит:** `feat(bot): смена экрана при отправке нового сообщения`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/090_P051_perehvat-otpravok-i-smena-ekrana.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Сообщения отправляются из ~15 модулей (handlers/*, alerts.py, moderation.py, preview.py, reports/sender.py). Не меняй их: перехватывай все исходящие отправки в одном месте — middleware сессии aiogram (`bot.session.middleware(...)`, класс на основе `aiogram.client.session.middlewares.base.BaseRequestMiddleware`).

1. **Номер экрана** (app/bot/screen.py):
   - `_SCREEN: ContextVar[str | None]` (по умолчанию None);
   - `new_screen() -> AbstractContextManager[str]` — ставит новый уникальный токен (uuid4().hex) и восстанавливает прежнее значение на выходе;
   - `current_screen() -> str` — текущий токен; если его нет (отправка вне new_screen), создаёт одноразовый токен, то есть такая отправка сама считается новым экраном.
2. **`ScreenTracker`:**
   - конструктор: session_scope, screen_repo (Protocol с register и forget из шага 089), bot (для удаления), cfg (UiConfig), clock;
   - `async on_sent(chat_id: int, message_ids: Sequence[int], token: str) -> None` — под asyncio.Lock этого чата (словарь блокировок по chat_id) вызывает register и удаляет вернувшиеся id: `bot.delete_messages(chat_id, ids)` пачками не больше `ui.delete_batch_size`; если пачка не удалилась (TelegramBadRequest) — по одному через `delete_message`.
   - Ошибки «message to delete not found» и «message can't be deleted» (сообщение старше 48 часов или уже удалено вручную) — это норма: лог `screen.delete_skipped` на уровне info, id убираются через forget. Остальные ошибки Telegram и ошибки БД — `log.exception("screen.delete_failed")`, исключение не пробрасывается: смена экрана не должна ломать отправку.
3. **`ScreenRequestMiddleware(tracker, channel_id, cfg)`** — middleware запросов:
   - вызывает `make_request`, затем, если `cfg.single_screen` включён и метод — один из SendMessage, SendPhoto, SendDocument, SendAnimation, SendVideo, SendMediaGroup, — передаёт в `tracker.on_sent` id отправленных сообщений (у SendMediaGroup — список) и `current_screen()`;
   - учитываются только личные чаты: chat_id > 0 и chat_id != channel_id. Канал и группы не трогаются никогда — посты в канале удаляться не должны;
   - результат запроса возвращается без изменений; ошибка трекера не влияет на результат.
4. **Конфигурация:** новая секция `ui` (UiConfig, extra="forbid", frozen) в app/config.py и configs/base.yaml: `single_screen: true`, `delete_user_messages: true` (используется в шаге 091), `delete_batch_size: 100` (лимит deleteMessages в Bot API).

**Критерий готовности:** При включённом single_screen каждое сообщение нового экрана удаляет весь прошлый экран в этом чате; сообщения одного экрана копятся вместе; канал не затрагивается; при выключенном флаге поведение прежнее.

**Граничные случаи:** Два сообщения в одном new_screen -> оба остаются; отправка без new_screen -> отдельный экран; SendMediaGroup -> все сообщения альбома в экране; отправка в канал -> ни register, ни удаления; прошлое сообщение старше 48 часов -> удаление пропущено без ошибки, новое сообщение отправлено; ошибка БД в register -> отправка всё равно успешна; параллельные отправки в один чат обрабатываются по очереди (Lock), в разные чаты — независимо; больше 100 сообщений к удалению -> несколько пачек.

**Тесты** (`tests/unit/test_bot_screen.py`): new_screen и current_screen (вложенность, восстановление); ScreenTracker на fakes (repo в памяти, fake bot с delete_messages и delete_message) — все граничные случаи; ScreenRequestMiddleware с fake make_request для каждого метода, канала, группы и выключенного флага; `tests/unit/test_config.py` — секция ui.

**Контекст проекта:** Python 3.11+, aiogram 3.x. Метод `bot.delete_messages` (deleteMessages) появился в aiogram 3.4: проверь установленную версию (`.venv/bin/pip show aiogram`), а не pyproject.toml, где стоит только `aiogram>=3`. Если версия ниже 3.4 — удаляй по одному через delete_message и упомяни это в отчёте; поднимать нижнюю границу зависимости в этом шаге не нужно. Подключение middleware к боту и новые экраны для входящих апдейтов — шаги 091–092.

**Файлы:** app/bot/screen.py, app/config.py, configs/base.yaml, tests/unit/test_bot_screen.py, tests/unit/test_config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токен бота не попадает в логи. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: ui.single_screen = true, ui.delete_user_messages = true, ui.delete_batch_size = 100.

**Формат ответа:** Полные файлы: app/bot/screen.py, app/config.py, configs/base.yaml, tests/unit/test_bot_screen.py, tests/unit/test_config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(bot): смена экрана при отправке нового сообщения"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
