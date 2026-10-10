# Шаг 038 из 070 · P018 · Этап 6 — Обработка через DeepSeek

**Задача трекера:** Клиент DeepSeek с лимитами и повторами
**Результат:** app/pipeline/ai/client.py: клиент DeepSeek
**Коммит:** `feat(ai): клиент deepseek с семафором и повторами`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/038_P018_klient-deepseek-s-semaforom-i-povtorami.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй DeepSeekClient(settings, cfg, openai_client=None): обёртка над openai.AsyncOpenAI с base_url = deepseek_base_url; chat_json(messages) -> AiRawResponse(content, prompt_tokens, completion_tokens) с response_format={type: json_object}, моделью, temperature и таймаутом из конфигурации; не больше max_concurrency одновременных запросов (asyncio.Semaphore); повторы tenacity на RateLimitError, APIConnectionError, APITimeoutError, InternalServerError; AuthenticationError и BadRequestError -> AiFatalError без повторов; исчерпание -> AiUnavailableError.

**Критерий готовности:** Клиент отправляет корректный запрос и возвращает текст и токены; ошибки классифицированы.

**Граничные случаи:** Пустой content -> AiResponseError; usage отсутствует -> токены 0 и предупреждение в логе; ключ API не попадает в логи.

**Тесты** (`tests/unit/test_ai_client.py`): параметры запроса, повтор на 429, отказ на 401, исчерпание повторов, лимит параллельности, пустой ответ (подменённый openai-клиент).

**Контекст проекта:** Python 3.11+, openai (AsyncOpenAI), tenacity.

**Файлы:** app/pipeline/ai/client.py, tests/unit/test_ai_client.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: ai.model — задаёт человек (сверить с документацией DeepSeek); ai.temperature = 0.3; ai.timeout_s = 60; ai.max_concurrency = 3; ai.max_retries = 3.

**Формат ответа:** Полные файлы: app/pipeline/ai/client.py, tests/unit/test_ai_client.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(ai): клиент deepseek с семафором и повторами"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
