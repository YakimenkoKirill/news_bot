# Шаг 013 из 070 · P007 · Этап 2 — База данных

**Задача трекера:** Репозитории доступа к данным
**Результат:** app/domain/status.py: машина состояний новости
**Коммит:** `feat(domain): машина состояний статусов новости`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/013_P007_mashina-sostoyaniy-statusov-novosti.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/domain/status.py: словарь ALLOWED_TRANSITIONS по жизненному циклу new -> duplicate | filtered | normalized; normalized -> processed | filtered | failed; processed -> pending_review | approved | filtered; pending_review -> approved | rejected; approved -> scheduled | published | failed; scheduled -> published | failed; failed -> approved (ручной повтор). Функции can_transition(old, new) -> bool и ensure_transition(old, new), которая бросает InvalidTransition с обоими статусами.

**Критерий готовности:** Разрешены ровно переходы из списка; все остальные бросают InvalidTransition.

**Граничные случаи:** Переход в тот же статус запрещён; терминальные статусы (duplicate, filtered, rejected, published) не имеют исходящих переходов.

**Тесты** (`tests/unit/test_status.py`): полная матрица NewsStatus x NewsStatus, сообщение исключения, терминальные статусы.

**Контекст проекта:** Python 3.11+.

**Файлы:** app/domain/status.py, tests/unit/test_status.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены.

**Формат ответа:** Полные файлы: app/domain/status.py, tests/unit/test_status.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(domain): машина состояний статусов новости"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
