# Шаг 023 из 070 · P010 · Этап 3 — Сборщики: RSS и сайты

**Задача трекера:** Сборщик сайтов без RSS
**Результат:** app/collectors/web.py: WebCollector
**Коммит:** `feat(collectors): сборщик сайтов без rss`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/023_P010_sborschik-saytov-bez-rss.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/collectors/web.py: WebCollector(http_client, robots, is_known_url) — проверяет robots.txt для страницы-списка, извлекает ссылки (extract_links), отбрасывает известные (is_known_url — асинхронная функция, в бою NewsRepository.exists_url), берёт не больше max_articles_per_run, для каждой проверяет robots.txt, загружает и разбирает статью и возвращает RawItem с external_id = URL статьи.

**Критерий готовности:** Новые статьи с сайта-фикстуры превращаются в RawItem; известные не загружаются повторно.

**Граничные случаи:** Запрет в robots.txt для списка -> PermanentCollectorError; запрет для статьи -> статья пропускается; ошибка одной статьи не роняет остальные; источник без selectors -> PermanentCollectorError.

**Тесты** (`tests/unit/test_web_collector.py`): успешный сбор, известные ссылки, лимит статей, запрет robots, ошибка одной статьи (aioresponses, фейковый robots).

**Контекст проекта:** Python 3.11+, aiohttp, aioresponses.

**Файлы:** app/collectors/web.py, tests/unit/test_web_collector.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: collectors.max_articles_per_run = 10.

**Формат ответа:** Полные файлы: app/collectors/web.py, tests/unit/test_web_collector.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(collectors): сборщик сайтов без rss"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
