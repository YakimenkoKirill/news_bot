# Шаг 027 из 070 · P013 · Этап 4 — Сборщик Telegram-каналов

**Задача трекера:** Преобразование сообщений каналов в новости
**Результат:** app/collectors/telegram/convert.py: сообщение -> RawItem
**Коммит:** `feat(telegram): преобразование сообщения канала в новость`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/027_P013_preobrazovanie-soobscheniya-kanala-v-novost.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй message_to_raw_item(message, source, channel_username) -> RawItem | None: текст из message.message, ссылки из сущностей MessageEntityTextUrl и MessageEntityUrl в raw_meta[`links`], external_id = `{channel_id}:{message_id}`, raw_meta[`msg_id`] = id сообщения, url = https://t.me/{username}/{id}, raw_meta[`has_photo`], published_at = message.date в UTC, title — первая строка текста, обрезанная до title_max_chars.

**Критерий готовности:** Сообщения с текстом превращаются в корректные RawItem со ссылкой на пост.

**Граничные случаи:** Нет текста и нет фото -> None; канал без username -> PermanentCollectorError (читаем только публичные каналы); сущности считаются по UTF-16 смещениям, как в Telegram.

**Тесты** (`tests/unit/test_telegram_convert.py`): текст со ссылками, эмодзи перед ссылкой (смещения UTF-16), только фото, пустое сообщение, канал без username (объекты-фейки через SimpleNamespace).

**Контекст проекта:** Python 3.11+, Telethon tl.types.

**Файлы:** app/collectors/telegram/convert.py, tests/unit/test_telegram_convert.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: telegram.title_max_chars = 120.

**Формат ответа:** Полные файлы: app/collectors/telegram/convert.py, tests/unit/test_telegram_convert.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(telegram): преобразование сообщения канала в новость"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
