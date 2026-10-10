# Шаг 043 из 070 · P023 · Этап 6 — Обработка через DeepSeek

**Задача трекера:** Процессор ИИ и стадия обработки
**Результат:** app/pipeline/ai/processor.py: обработка одной новости
**Коммит:** `feat(ai): процессор новости через deepseek`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/043_P023_protsessor-novosti-cherez-deepseek.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй AiProcessor(client, profile, repos, cfg).process(news, source) -> NewsStatus: build_messages -> chat_json -> parse_ai_response (при AiResponseError один повтор) -> unsupported_facts -> сохранение ProcessedPost (prompt_version, токены, cost). Итог: relevant=false или score < min_score -> filtered (reason not_relevant); есть неподтверждённые факты -> pending_review с review_note; иначе processed, затем pending_review при moderation_mode=review или approved при auto. Все смены статуса — через set_status.

**Критерий готовности:** Каждая новость получает ProcessedPost и корректный итоговый статус.

**Граничные случаи:** Повторный AiResponseError -> failed с reason=bad_ai_response; AiUnavailableError пробрасывается (новость остаётся normalized); AiFatalError пробрасывается.

**Тесты** (`tests/unit/test_ai_processor.py`): auto и review, нерелевантная, низкий score, выдуманный факт, повтор после плохого ответа, недоступность API (фейковый клиент).

**Контекст проекта:** Python 3.11+.

**Файлы:** app/pipeline/ai/processor.py, tests/unit/test_ai_processor.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: ai.min_score — задаёт человек.

**Формат ответа:** Полные файлы: app/pipeline/ai/processor.py, tests/unit/test_ai_processor.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(ai): процессор новости через deepseek"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
