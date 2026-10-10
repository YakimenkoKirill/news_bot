# Шаг 022 из 070 · P010 · Этап 3 — Сборщики: RSS и сайты

**Задача трекера:** Сборщик сайтов без RSS
**Результат:** app/collectors/web_article.py: текст, картинка и дата статьи
**Коммит:** `feat(collectors): разбор статьи через trafilatura`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/022_P010_razbor-stati-cherez-trafilatura.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/collectors/web_article.py: parse_article(html, url) -> ArticleData | None. Текст — trafilatura.extract без комментариев и таблиц; заголовок — og:title или <title>; картинка — og:image (абсолютный URL); дата — article:published_time или <time datetime>, приведённая к UTC.

**Критерий готовности:** Для фикстур статей возвращаются заголовок, чистый текст, картинка и дата.

**Граничные случаи:** Текст короче collectors.min_article_chars -> None; нет даты -> published_at None; некорректная дата не роняет разбор.

**Тесты** (`tests/unit/test_web_article.py`): полная статья, статья без картинки и даты, слишком короткий текст, некорректная дата.

**Контекст проекта:** Python 3.11+, trafilatura, BeautifulSoup4.

**Файлы:** app/collectors/web_article.py, tests/fixtures/web/article_*.html, tests/unit/test_web_article.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: collectors.min_article_chars = 200.

**Формат ответа:** Полные файлы: app/collectors/web_article.py, tests/fixtures/web/article_*.html, tests/unit/test_web_article.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(collectors): разбор статьи через trafilatura"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
