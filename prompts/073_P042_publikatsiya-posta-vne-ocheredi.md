# Шаг 073 из 077 · P042 · Этап 11 — Очередь публикации в боте управления

**Задача трекера:** Очередь подготовленных постов: приоритет по интересу и публикация вне очереди
**Результат:** publish_item и publish_now в app/publisher/stage_publish.py
**Коммит:** `feat(publisher): публикация поста вне очереди`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/073_P042_publikatsiya-posta-vne-ocheredi.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Дай возможность опубликовать конкретный пост сразу, вне очереди, через тот же код, что и обычная публикация.

1. Переименуй `_publish` в публичную `publish_item(item, now, session, deps) -> PublishReport`, поведение не меняй; publish_due вызывает её. Сборка поста, проверка картинки, лимиты длины, запись Publication, смена статуса и алерт publish_error остаются только в ней, без копий.
2. Добавь `MANUAL_STATUSES = (NewsStatus.APPROVED, NewsStatus.SCHEDULED, NewsStatus.PENDING_REVIEW)`.
3. Добавь Protocol `ManualNewsRepo(PublishNewsRepo)` с методами `claim_one(news_id, statuses)` (шаг 071) и `get(news_id)`. PublishNewsRepo (с claim_next после шага 072) не расширяй, чтобы fakes в tests/unit/test_stage_publish.py оставались валидными для mypy.
4. Добавь `@dataclass(frozen=True) ManualPublishDeps`: `publish: PublishDeps` и `news_repo: Callable[[Any], ManualNewsRepo]`.
5. Добавь `ManualOutcome(StrEnum)`: `published`, `not_found`, `busy`, `already_done`, `failed`, и `@dataclass(frozen=True) ManualPublishResult`: outcome, news_id, message_id (int | None), status (NewsStatus | None — текущий статус для already_done), error (str | None).
6. Реализуй `publish_now(news_id: int, now: datetime, deps: ManualPublishDeps, initiator_id: int) -> ManualPublishResult`, всё в одной транзакции session_scope:
   - `claim_one(news_id, MANUAL_STATUSES)`. Если получили None, сделай `get(news_id)`: новости нет -> not_found; её статус входит в MANUAL_STATUSES -> busy (строку держит другая транзакция, скорее всего publish_due); иначе -> already_done с текущим статусом.
   - Если новость в pending_review, сначала `set_status(id, PENDING_REVIEW, APPROVED)`: прямого перехода pending_review -> published в машине состояний нет, и обходить её нельзя. set_status обновляет объект в сессии (populate_existing), поэтому дальше item.status == approved.
   - Вызови publish_item. Если в отчёте есть error -> failed (новость уже в failed, алерт publish_error уже отправлен), иначе -> published с message_id.
   - Запиши в лог событие `publish.manual` с полями news_id, initiator_id, outcome.

Что publish_now не учитывает: приоритет очереди (оценку интереса и свежесть), publisher.min_interval_min, тихие часы и паузу (настройку paused). Это явное решение администратора. Что учитывает: лимиты длины, проверку картинки, машину состояний и блокировку строки. Отдельный код для «сдвига» очереди не нужен: Publication(sent) обновляет last_published_at, и следующий слот publish_due сам отодвигается на min_interval_min.

**Критерий готовности:** Пост публикуется сразу при закрытом слоте, в тихие часы и на паузе; повторное нажатие и гонка с publish_due не приводят к двойной публикации; publish_due ведёт себя как раньше.

**Граничные случаи:** pending_review -> approved -> published (оба перехода выполнены); новость уже published -> already_done со статусом published, без вызова publisher.send; строка занята -> busy без ожидания; несуществующий id -> not_found; PublishError -> failed, статус failed, Publication(failed), алерт publish_error; после ручной публикации publish_due в тот же момент возвращает skipped="slot" и next_slot = now + min_interval_min.

**Тесты** (`tests/unit/test_stage_publish_now.py`): fakes по образцу tests/unit/test_stage_publish.py (скопируй нужные; claim_one учитывает множество «занятых» id); все граничные случаи выше; публикация в тихие часы, на паузе и через минуту после предыдущего поста. Отдельный тест: ручная публикация поста с наименьшим приоритетом не меняет порядок остальных — следующий publish_due после окончания интервала берёт пост с наибольшим приоритетом. tests/unit/test_stage_publish.py не меняй — он должен проходить как есть.

**Контекст проекта:** Python 3.11+, aiogram 3.x. publish_now вызывается из бота управления на шаге 076 и подключается в app/main.py на шаге 077.

**Файлы:** app/publisher/stage_publish.py, tests/unit/test_stage_publish_now.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Смена статуса — только через set_status и app/domain/status.py; ALLOWED_TRANSITIONS не меняй. В канал уходит только пересказ со ссылкой на источник (инвариант 8). Новые зависимости, новые ключи конфигурации, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй и не меняй.

**Формат ответа:** Полные файлы: app/publisher/stage_publish.py, tests/unit/test_stage_publish_now.py. Тесты обязательны. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(publisher): публикация поста вне очереди"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
