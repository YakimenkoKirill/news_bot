# Шаг 098 из 113 · P054 · Этап 16 — Надёжность и расходы на модель

**Задача трекера:** Надёжность публикации и отчёта
**Результат:** Публикация в три коротких шага: захват -> отправка вне транзакции -> запись итога; застрявшие отправки не публикуются повторно
**Коммит:** `fix(publisher): отправка поста вне транзакции и защита от двойной публикации`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/098_P054_publikatsiya-bez-dolgoy-tranzaktsii.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Сейчас publish_item (app/publisher/stage_publish.py) держит транзакцию и блокировку строки новости, пока идут проверка картинки по HTTP и отправка в Telegram с повторами (в том числе ожидание RetryAfter). Это занимает соединение пула надолго, а если пост ушёл в канал, но запись в БД после этого упала, новость остаётся в очереди и выйдет второй раз.

Статус `scheduled` сейчас нигде не ставится — используй его как «отправляется».

1. **Публикация в три шага** (publish_due и publish_now):
   1. Транзакция 1 (короткая): захват (claim_next / claim_one), для pending_review в publish_now — переход в approved, затем `approved -> scheduled`; загрузить всё нужное для поста (пересказ, источник) в обычные dataclass'ы, не ORM-объекты; commit.
   2. Без транзакции: check_image, сборка поста, `publisher.send`.
   3. Транзакция 2 (короткая): Publication(sent) и `scheduled -> published` или Publication(failed), `scheduled -> failed` с reason publish_error и алерт publish_error.
   - Для «нет пересказа или источника» — `scheduled -> failed` с reason no_processed_post в транзакции 1 или 2, без отправки.
2. **Очередь:** PUBLISHABLE_STATUSES = (APPROVED,) — посты в scheduled уже отправляются и не захватываются повторно. MANUAL_STATUSES = (APPROVED, PENDING_REVIEW) — «Опубликовать сейчас» для поста в scheduled отвечает busy. Лимит источника (QUEUE_STATUSES в app/pipeline/queue_limit.py) по-прежнему считает approved и scheduled — отправляемый пост занимает место; этот файл не меняй. В /queue (вкладка «В очереди») пост в scheduled помечается «⏳ отправляется».
3. **Зависшие отправки:** если приложение упало между шагами 2 и 3, пост остаётся в scheduled без Publication(sent), и неизвестно, ушёл ли он в канал. Функция `recover_stuck(now, deps) -> int` в stage_publish.py: новости в scheduled дольше publisher.stuck_after_min минут без Publication(sent) -> `scheduled -> failed` с reason `publish_unknown` и алерт publish_error «Проверьте канал: пост мог выйти; если не вышел — /errors -> Новости -> Повторить». Автоматически повторно не отправлять. Вызывать на старте (компонент рядом с QueueTrimComponent в app/main.py) и раз в scheduler.recover_every_min минут.
4. **/errors:** причина publish_unknown в карточке новости — «неизвестно, вышел ли пост: проверьте канал».

Машина состояний: нужные переходы approved -> scheduled, scheduled -> published, scheduled -> failed уже есть; не добавляй новых.

**Критерий готовности:** Ни одна транзакция не открыта во время сетевых вызовов публикации; при падении между отправкой и записью пост не публикуется второй раз автоматически, а админ получает предупреждение.

**Граничные случаи:** PublishError -> failed, Publication(failed), алерт; RetryAfter во время отправки не держит соединение БД; publish_now для поста в scheduled -> busy; два вызова publish_due подряд, пока первый отправляет, -> второй не берёт тот же пост; recover_stuck не трогает свежие scheduled (моложе stuck_after_min) и посты, у которых есть Publication(sent) (тогда scheduled -> published); лимит источника учитывает scheduled.

**Тесты:** `tests/unit/test_stage_publish.py` и `tests/unit/test_stage_publish_now.py` — обнови fakes под трёхшаговую схему, проверь, что fake session_scope не открыт во время send (флаг в fake), все исходы; `tests/unit/test_stage_publish_recover.py` — recover_stuck; `tests/integration/test_publish_flow.py` (маркер integration) — реальная БД, fake publisher, падение после send (исключение в транзакции 2) -> пост в scheduled, затем recover_stuck -> failed/publish_unknown; `tests/unit/test_scheduler.py` — новая задача. Допустимо обновить ожидания старых тестов только там, где они проверяли одну общую транзакцию.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async), aiogram 3.x. После шага 095 publish_now и admit_to_queue уже есть — порядок переходов в них сохрани.

**Файлы:** app/publisher/stage_publish.py, app/bot/handlers/queue.py, app/bot/handlers/errors.py, app/bot/texts.py, app/scheduler.py, app/main.py, app/config.py, configs/base.yaml, тестовые файлы из списка выше.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). Смена статуса новости — только через set_status и app/domain/status.py. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Если меняется состав или порядок компонентов в app/main.py — обнови ожидания tests/unit/test_app_wiring.py; новая секция конфигурации должна быть описана в README.md (это проверяет tests/unit/test_readme.py). Новые ключи конфигурации добавь в configs/base.yaml и в типизированную секцию модели в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: publisher.stuck_after_min = 15, scheduler.recover_every_min = 10.

**Формат ответа:** Полные файлы из списка «Файлы». Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "fix(publisher): отправка поста вне транзакции и защита от двойной публикации"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
