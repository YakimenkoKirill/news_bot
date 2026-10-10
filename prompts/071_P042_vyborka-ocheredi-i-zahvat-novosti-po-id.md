# Шаг 071 из 077 · P042 · Этап 11 — Очередь публикации в боте управления

**Задача трекера:** Очередь подготовленных постов: приоритет по интересу и публикация вне очереди
**Результат:** Приоритет очереди и методы NewsRepository: list_prepared, count_prepared, claim_next, claim_one
**Коммит:** `feat(db): очередь публикации по приоритету и захват новости по id`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/071_P042_vyborka-ocheredi-i-zahvat-novosti-po-id.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Подготовь в NewsRepository (app/db/repositories/news.py) очередь публикации, упорядоченную по интересу, и захват конкретного поста для публикации из бота. Существующие методы, в том числе claim_batch, не меняй: им пользуются стадии нормализации и ИИ, где порядок «старые первыми» нужен по-прежнему.

**Приоритет поста.** `priority = processed_posts.relevance_score − score_decay_per_h × возраст_в_часах`, где возраст считается от `coalesce(news_items.published_at, news_items.created_at)`. Оценка — это `score` модели (0…1); поправка на возраст нужна, чтобы интересная, но вчерашняя новость не обгоняла свежую. Так как `−decay × now` одинаков для всех постов, сортировку выполняй по выражению без текущего времени: `relevance_score + score_decay_per_h × epoch(coalesce(published_at, created_at)) / 3600` по убыванию, при равенстве — id по возрастанию. Тогда порядок не меняется со временем, а меняется, только когда приходят новые посты. Выражение собери один раз в приватной функции `_priority(decay_per_h)` и используй во всех методах ниже. decay_per_h = 0 означает сортировку только по оценке.

1. `list_prepared(statuses: Sequence[NewsStatus], decay_per_h: float, limit: int, offset: int) -> list[tuple[NewsItem, ProcessedPost]]` — новости в одном из статусов вместе с пересказом: один SELECT обеих сущностей с INNER JOIN processed_posts по news_item_id (у связей lazy="raise", поэтому не обращайся к news.processed_post, а выбирай обе сущности явно). Порядок — по приоритету. Без блокировок строк.
2. `count_prepared(statuses: Sequence[NewsStatus]) -> int` — количество тех же строк (тот же фильтр и тот же JOIN).
3. `claim_next(statuses: Sequence[NewsStatus], decay_per_h: float) -> NewsItem | None` — пост с наибольшим приоритетом: тот же запрос, что в list_prepared, с `LIMIT 1` и `FOR UPDATE OF news_items SKIP LOCKED` (`with_for_update(skip_locked=True, of=NewsItem)`), `populate_existing=True`. Его вызовет publish_due на шаге 072.
4. `claim_one(news_id: int, statuses: Sequence[NewsStatus]) -> NewsItem | None` — `SELECT ... WHERE id = :news_id AND status IN (...) FOR UPDATE SKIP LOCKED` с `populate_existing=True`, как в claim_batch. Блокировка держится до конца транзакции вызывающего. Если строку уже держит другая транзакция (её прямо сейчас публикует publish_due), метод не ждёт и возвращает None.

**Критерий готовности:** Методы работают на реальном PostgreSQL; list_prepared и claim_next дают один и тот же порядок; при decay_per_h = 0 порядок — по убыванию оценки; claim_one и claim_next не блокируются на занятой строке.

**Граничные случаи:** Пустой список статусов -> ValueError (как в claim_batch); limit <= 0, offset < 0 или decay_per_h < 0 -> ValueError; offset за концом списка -> пустой список; новость без пересказа не попадает ни в один метод; published_at = NULL -> возраст от created_at; равные приоритеты -> меньший id первым; claim_next пропускает занятую строку и берёт следующую; claim_one для новости в другом статусе или несуществующего id -> None; строка, захваченная в параллельной сессии, -> claim_one возвращает None сразу, без ожидания.

**Тесты** (`tests/integration/test_repo_news_queue.py`, маркер integration): порядок по оценке при decay = 0; свежая новость с оценкой 0,6 обгоняет новость с оценкой 0,8 двенадцатичасовой давности при decay = 0,02 и не обгоняет при decay = 0; published_at = NULL; одинаковый приоритет; пагинация list_prepared (limit/offset) и совпадение порядка с последовательными claim_next; фильтр по нескольким статусам; исключение новостей без пересказа; count_prepared; claim_next и claim_one при занятой строке (две сессии из фикстур tests/integration/conftest.py: первая держит захват в открытой транзакции, вторая вызывает метод); ValueError на неверных аргументах.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async), PostgreSQL 16. Схема БД не меняется, новая миграция не нужна. Ключ конфигурации publisher.score_decay_per_h появится на шаге 072; здесь коэффициент — обычный аргумент метода. Методы нужны шагам 072 (приоритет в publish_due), 073 (публикация вне очереди) и 075 (список /queue).

**Файлы:** app/db/repositories/news.py, tests/integration/test_repo_news_queue.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Смена статуса — только через set_status и app/domain/status.py; в этом шаге статусы не меняются. Новые зависимости, миграции, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Сигнатуры и поведение существующих методов NewsRepository не меняй.

**Формат ответа:** Полные файлы: app/db/repositories/news.py, tests/integration/test_repo_news_queue.py. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(db): очередь публикации по приоритету и захват новости по id"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
