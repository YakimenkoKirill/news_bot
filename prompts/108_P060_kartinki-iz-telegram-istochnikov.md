# Шаг 108 из 113 · P060 · Этап 18 — Качество канала

**Задача трекера:** Картинки для постов из Telegram
**Результат:** Посты из Telegram-источников выходят с фото исходного сообщения
**Коммит:** `feat(publisher): фото из сообщений Telegram-источников`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/108_P060_kartinki-iz-telegram-istochnikov.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Для Telegram-новостей image_url пустой: у фото в Telegram нет публичного URL. Но в raw_meta уже есть msg_id, channel_id и has_photo (app/collectors/telegram/convert.py).

1. **Загрузка фото:** `TelegramCollector.download_photo(source_url: str, msg_id: int, max_bytes: int) -> bytes | None` — get_messages по каналу источника, `download_media(message, file=bytes)` для фото (для альбома — первое фото из raw_meta.photo_ids, если они есть); больше max_bytes (publisher.image_max_bytes) или не фото -> None. Таймаут и повторы — как у остальных сетевых вызовов коллектора.
2. **Протокол** `PhotoFetcher` в app/publisher/media.py: `async fetch(news) -> bytes | None` — для источника tg с raw_meta.has_photo вызывает download_photo, иначе None. Реализация собирается в app/main.py поверх TelegramCollector.
3. **Публикатор:** `Publisher.send(fitted, photo: str | bytes | None)` — bytes отправляются как `BufferedInputFile(data, filename="photo.jpg")`, строка — как URL (как раньше). fit_post получает has_photo = True и для bytes (лимит подписи 1024).
4. **Где вызывается:** в шаге «отправка вне транзакции» публикации (шаг 098) — если image_url пустой, `photo = await fetcher.fetch(news)`; ошибка загрузки -> лог `publish.tg_photo_failed`, пост уходит текстом (не падает).
5. **Предпросмотр:** app/bot/preview.py использует тот же fetcher, чтобы черновик в боте выглядел как пост в канале.

**Критерий готовности:** Пост из Telegram-источника с фото выходит в канал с этим фото; при любой проблеме с загрузкой — текстом, как раньше.

**Граничные случаи:** Сообщение удалено в источнике -> текстом; фото больше 5 МБ -> текстом; видео вместо фото -> текстом; альбом -> первое фото; Telethon отключён -> текстом и лог; RSS и сайты -> без изменений (URL).

**Тесты:** `tests/unit/test_photo_fetcher.py` — fake Telethon: фото, альбом, удалено, размер, не фото, ошибка сети; `tests/unit/test_publisher.py` — отправка bytes как BufferedInputFile и URL как раньше; `tests/unit/test_stage_publish.py` — fetcher вызывается только без image_url, ошибка -> текст; `tests/unit/test_bot_preview.py` — предпросмотр с bytes.

**Контекст проекта:** Python 3.11+, Telethon, aiogram 3.x. Шаг 098 вынес отправку из транзакции — загрузка фото тоже вне транзакции.

**Файлы:** app/collectors/telegram/collector.py, app/publisher/media.py, app/publisher/publisher.py, app/publisher/stage_publish.py, app/bot/preview.py, app/main.py, тестовые файлы из списка выше.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). Смена статуса новости — только через set_status и app/domain/status.py. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Если меняется состав или порядок компонентов в app/main.py — обнови ожидания tests/unit/test_app_wiring.py; новая секция конфигурации должна быть описана в README.md (это проверяет tests/unit/test_readme.py). Новых ключей конфигурации не добавляй: лимит размера — publisher.image_max_bytes.

**Формат ответа:** Полные файлы из списка «Файлы». Тесты обязательны; проверка: `make lint`, `make test`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(publisher): фото из сообщений Telegram-источников"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
