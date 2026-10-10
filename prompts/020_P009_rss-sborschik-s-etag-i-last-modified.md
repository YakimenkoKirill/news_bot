# Шаг 020 из 070 · P009 · Этап 3 — Сборщики: RSS и сайты

**Задача трекера:** RSS-сборщик с условными запросами
**Результат:** app/collectors/rss.py: RSS/Atom-сборщик
**Коммит:** `feat(collectors): rss-сборщик с etag и last-modified`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/020_P009_rss-sborschik-s-etag-i-last-modified.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/collectors/rss.py: RssCollector(http_client) — условный GET с If-None-Match и If-Modified-Since из source.fetch_state; разбор ответа feedparser.parse(bytes); external_id = entry.id или entry.link; published_at из published_parsed/updated_parsed в UTC; image_url из media_content, media_thumbnail или enclosure с типом image/*. Метод возвращает новости и новый fetch_state (etag, last_modified).

**Критерий готовности:** Лента RSS 2.0 и Atom превращается в список RawItem; при 304 возвращается пустой список и прежний fetch_state.

**Граничные случаи:** Битая лента (bozo) с записями обрабатывается; bozo без записей -> TemporaryCollectorError; запись без ссылки пропускается; дата без часового пояса считается UTC.

**Тесты** (`tests/unit/test_rss_collector.py`): RSS 2.0, Atom, 304, заголовки условного запроса, картинка из enclosure, битая лента (фикстуры в tests/fixtures/rss/).

**Контекст проекта:** Python 3.11+, feedparser, aioresponses.

**Файлы:** app/collectors/rss.py, tests/fixtures/rss/*.xml, tests/unit/test_rss_collector.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/collectors/rss.py, tests/fixtures/rss/*.xml, tests/unit/test_rss_collector.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(collectors): rss-сборщик с etag и last-modified"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
