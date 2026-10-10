# Шаг 049 из 070 · P026 · Этап 7 — Публикатор в канал

**Задача трекера:** Очередь публикаций и отправка в канал
**Результат:** app/publisher/publisher.py: отправка в канал
**Коммит:** `feat(publisher): отправка постов в канал через aiogram`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/049_P026_otpravka-postov-v-kanal-cherez-aiogram.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй Publisher(bot, channel_id, cfg).send(fitted, image_url) -> int (message_id): при use_photo — send_photo(caption, parse_mode=HTML), иначе send_message с LinkPreviewOptions(url=ссылка источника, prefer_large_media=True). TelegramRetryAfter -> ожидание retry_after и повтор не больше max_retries; TelegramBadRequest при отправке фото -> одна повторная отправка текстом; прочие ошибки -> PublishError с текстом ошибки.

**Критерий готовности:** Пост уходит в канал и возвращается message_id; ограничения Telegram соблюдаются.

**Граничные случаи:** Исчерпание повторов RetryAfter -> PublishError; ошибка текста после отказа фото -> PublishError; токен бота не попадает в логи.

**Тесты** (`tests/unit/test_publisher.py`): фото, текст с превью, RetryAfter, отказ фото с переходом на текст, прочая ошибка (мок aiogram.Bot).

**Контекст проекта:** Python 3.11+, aiogram 3.x.

**Файлы:** app/publisher/publisher.py, tests/unit/test_publisher.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: publisher.max_retries = 3.

**Формат ответа:** Полные файлы: app/publisher/publisher.py, tests/unit/test_publisher.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(publisher): отправка постов в канал через aiogram"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
