# Шаг 017 из 070 · P008 · Этап 3 — Сборщики: RSS и сайты

**Задача трекера:** Базовый интерфейс сборщика, HTTP-клиент и robots.txt
**Результат:** app/collectors/base.py: RawItem, BaseCollector, ошибки
**Коммит:** `feat(collectors): базовый интерфейс сборщика`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/017_P008_bazovyy-interfeys-sborschika.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/collectors/base.py: RawItem (pydantic, frozen) с полями source_id, external_id, url, title, text, image_url, published_at, raw_meta; абстрактный BaseCollector с методом async fetch(source) -> list[RawItem]; иерархия ошибок CollectorError -> TemporaryCollectorError (повторяемая) и PermanentCollectorError (неповторяемая).

**Критерий готовности:** RawItem валидирует данные; сборщики наследуют BaseCollector и бросают только ошибки иерархии.

**Граничные случаи:** naive datetime в published_at -> ValidationError; пустой external_id -> ValidationError; url не http(s) -> ValidationError; text и title обрезаются от пробелов.

**Тесты** (`tests/unit/test_collector_base.py`): валидация каждого поля, frozen, иерархия ошибок, абстрактный класс нельзя создать.

**Контекст проекта:** Python 3.11+, pydantic 2.x, abc.

**Файлы:** app/collectors/base.py, tests/unit/test_collector_base.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/collectors/base.py, tests/unit/test_collector_base.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(collectors): базовый интерфейс сборщика"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
