# Шаг 075 из 077 · P043 · Этап 11 — Очередь публикации в боте управления

**Задача трекера:** Очередь подготовленных постов в боте управления
**Результат:** Команда /queue: вкладки «В очереди» и «На модерации», порядок по приоритету, оценка интереса, ожидаемое время выхода, предпросмотр поста
**Коммит:** `feat(bot): просмотр очереди подготовленных постов`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/075_P043_prosmotr-ocheredi-podgotovlennyh-postov.md». После коммита: `python prompts/next_step.py --done`.

## Задача

1. **Ожидаемое время выхода.** В app/publisher/slots.py добавь `queue_eta(now, last_published_at, count, min_interval_min, quiet_hours, tz) -> list[datetime]`: время выхода первых count постов очереди. Первый — `next_slot(now, last_published_at, ...)`, каждый следующий — `next_slot(предыдущий, предыдущий, ...)`. Это оценка: новые посты, пауза и сбои не учитываются. count = 0 -> пустой список.
2. **Клавиатуры** (app/bot/keyboards.py):
   - `QueueTab(StrEnum)`: `queue` — статусы approved и scheduled; `review` — pending_review. Сопоставление вкладки и статусов держи в одном месте (`QUEUE_TAB_STATUSES`).
   - `QueueActionType(StrEnum)`: `page`, `open`, `back`.
   - `QueueAction(CallbackData, prefix="q")`: action, tab, page, news_id (по умолчанию 0).
   - `queue_keyboard(positions: Sequence[tuple[int, int]], tab, page, has_prev, has_next)`: на каждый пост кнопка «👁 N», где N — его номер в очереди, а news_id идёт в callback; строка вкладок «В очереди» / «На модерации» (активная помечена «•»); навигация ◀️ ▶️.
   - `queue_item_keyboard(news_id, tab, page)`: пока одна кнопка «◀️ К списку» (кнопку публикации добавит шаг 076).
3. **Тексты** (app/bot/texts.py): заголовок «<b>Очередь публикации</b> · N постов · страница X из Y» (для вкладки review — «<b>На модерации</b> …»); пустая вкладка; строка вкладки queue: «{N}. {заголовок} — {источник} · ★{оценка} · ≈{ЧЧ:ММ}» (оценка — relevance_score с двумя знаками после запятой; время — в app.timezone, если не сегодня — «≈{ДД.ММ ЧЧ:ММ}»); строка вкладки review — с оценкой, без времени, с «⚠️», если у поста есть review_note; подпись под заголовком вкладки queue «Порядок — по интересу с поправкой на свежесть; новые интересные посты могут встать выше»; на паузе — строка «⏸ Пайплайн на паузе — очередь стоит», и время не показывается; «Пост уже не в очереди: {статус}». Заголовок обрезай `truncate_words` до bot.queue_title_chars, весь пользовательский текст экранируй html.escape.
4. **Обработчик** app/bot/handlers/queue.py:
   - `@dataclass(frozen=True) QueueDeps`: session_scope, news_repo, post_repo, publication_repo, settings_repo, source_repo, preview (PreviewDeps из шага 074), publisher_cfg (PublisherConfig), channel_id, tz, page_size (bot.page_size), title_chars (bot.queue_title_chars), clock. Нужные части репозиториев описывай через Protocol, как в других обработчиках.
   - `render_page(deps, tab, page, now) -> tuple[str, InlineKeyboardMarkup]`: count_prepared и list_prepared из шага 071 с offset = page * page_size и decay_per_h = publisher_cfg.score_decay_per_h — тот же порядок, в котором publish_due выбирает посты (шаг 072), поэтому номер в списке — это номер в очереди выхода; вкладка review упорядочена так же; номер страницы зажимается в допустимый диапазон, как в sources.render_page; для вкладки queue считай `queue_eta` на offset + len(страницы) постов и бери срез, чтобы время на второй и следующих страницах было верным; last_published_at — из PublicationRepository.
   - `/queue` -> вкладка queue, страница 0.
   - Callback `page` -> редактирует сообщение со списком (другая вкладка или страница).
   - Callback `open` -> загружает новость и пересказ; если новость уже не в статусах своей вкладки — callback.answer «Пост уже не в очереди: {статус}» с show_alert; иначе для поста с review_note сначала отправляет заметку (как BotReviewNotifier), затем `build_preview` + `send_preview` с queue_item_keyboard новым сообщением.
   - Callback `back` -> присылает список заново (та же вкладка и страница).
   - `create_router()` регистрирует /queue и `QueueAction.filter()` только для действий page, open, back.
5. **Конфигурация:** новый ключ bot.queue_title_chars = 60 — в configs/base.yaml и в BotConfig (app/config.py, Field(ge=1)).

**Критерий готовности:** /queue показывает подготовленные посты в том порядке, в котором они выйдут (по приоритету), с оценкой и ожидаемым временем; любой пост открывается предпросмотром ровно в том виде, в каком он появится в канале.

**Граничные случаи:** Пустая очередь -> текст «пусто» и только строка вкладок; страница за концом (посты успели уйти) -> последняя существующая; тихие часы -> время первого поста = их окончание; ETA на второй странице продолжает первую; пост опубликован между показом списка и нажатием «👁» -> alert «уже не в очереди»; callback_data каждой кнопки не длиннее 64 байт (проверь тестом при большом news_id); заголовок с HTML-символами экранирован.

**Тесты:** `tests/unit/test_slots.py` — дописать тесты queue_eta (интервал, тихие часы через полночь, count = 0, без предыдущей публикации); `tests/unit/test_bot_queue.py` — render_page (порядок по приоритету совпадает с порядком выбора publish_due, оценка в строке, пагинация, ETA, пауза, вкладка review с ⚠️, пустая очередь), callback page/open/back, «уже не в очереди», длина callback_data. Fakes — по образцу tests/unit/test_bot_sources.py.

**Контекст проекта:** Python 3.11+, aiogram 3.x. Роутер подключается в app/main.py на шаге 077; здесь только модуль и тесты.

**Файлы:** app/publisher/slots.py, app/bot/keyboards.py, app/bot/texts.py, app/bot/handlers/queue.py, configs/base.yaml, app/config.py, tests/unit/test_slots.py, tests/unit/test_bot_queue.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: bot.queue_title_chars = 60. texts.COMMANDS в этом шаге не меняй (это шаг 077).

**Формат ответа:** Полные файлы: app/publisher/slots.py, app/bot/keyboards.py, app/bot/texts.py, app/bot/handlers/queue.py, configs/base.yaml, app/config.py, tests/unit/test_slots.py, tests/unit/test_bot_queue.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(bot): просмотр очереди подготовленных постов"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
