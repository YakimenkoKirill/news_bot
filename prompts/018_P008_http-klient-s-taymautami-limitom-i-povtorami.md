# Шаг 018 из 070 · P008 · Этап 3 — Сборщики: RSS и сайты

**Задача трекера:** Базовый интерфейс сборщика, HTTP-клиент и robots.txt
**Результат:** app/collectors/http.py: HTTP-клиент с лимитами и повторами
**Коммит:** `feat(collectors): http-клиент с таймаутами, лимитом и повторами`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/018_P008_http-klient-s-taymautami-limitom-i-povtorami.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Реализуй app/collectors/http.py: HttpClient — одна aiohttp.ClientSession на процесс (async start/close), заголовок User-Agent из конфигурации, общий таймаут, ограничение запросов на домен (не чаще per_domain_rps), get(url, headers=None) -> HttpResponse(status, headers, body, url). Повторы через tenacity с экспоненциальной задержкой на 5xx, asyncio.TimeoutError и ClientConnectionError; 429 — ожидание по Retry-After; прочие 4xx -> PermanentCollectorError; исчерпание повторов -> TemporaryCollectorError.

**Критерий готовности:** Запросы к одному домену разнесены по времени согласно лимиту; ошибки классифицируются верно.

**Граничные случаи:** 304 возвращается как обычный ответ без повторов; Retry-After в виде даты тоже поддерживается; редиректы сохраняют итоговый url.

**Тесты** (`tests/unit/test_http_client.py`): User-Agent, повтор на 503, отказ на 404, 429 с Retry-After, исчерпание повторов, лимит на домен (подменённые часы), 304 (aioresponses).

**Контекст проекта:** Python 3.11+, aiohttp, tenacity, aioresponses.

**Файлы:** app/collectors/http.py, tests/unit/test_http_client.py, configs/base.yaml, app/config.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). .gitignore не менять. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: collectors.user_agent — задаёт человек (имя бота и контакт); collectors.http_timeout_s = 20; collectors.per_domain_rps = 1; collectors.max_retries = 3.

**Формат ответа:** Полные файлы: app/collectors/http.py, tests/unit/test_http_client.py, configs/base.yaml, app/config.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(collectors): http-клиент с таймаутами, лимитом и повторами"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
