# Шаг 114 из 117 · P065 · Этап 20 — Отмена постов на модерации

**Задача трекера:** Отмена постов на модерации
**Результат:** app/pipeline/review.py — отмена одного поста на модерации и всех сразу; модерация использует ту же функцию
**Коммит:** `feat(pipeline): отмена постов на модерации`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/114_P065_otmena-posta-na-moderatsii.md». После коммита: `python prompts/next_step.py --done`.

## Задача

Сейчас отклонить пост на модерации можно только кнопкой «🚫 Отклонить» под черновиком (app/bot/handlers/moderation.py, on_review). В режиме одного экрана черновик быстро заменяется следующим сообщением, а в /queue -> «На модерации» есть только «Опубликовать сейчас». Нужна функция отмены, которую вызовут и модерация, и /queue (шаг 115).

1. **Модуль** app/pipeline/review.py:
   - `REJECT_REASON = "rejected_by_moderator"` — перенеси константу сюда из handlers/moderation.py (там оставь импорт, чтобы существующие ссылки работали);
   - `@dataclass(frozen=True) ReviewDeps`: session_scope, news_repo (Protocol: get, set_status, claim_batch), post_repo (Protocol: get_by_news, set_snooze);
   - `CancelOutcome(StrEnum)`: `cancelled`, `already_done`, `not_found`; `CancelResult`: outcome и текущий статус (NewsStatus | None).
2. **`cancel_review(news_id: int, by: int, deps) -> CancelResult`** — одна транзакция: новости нет -> not_found; статус не pending_review -> already_done с текущим статусом; иначе `set_status(PENDING_REVIEW -> REJECTED, reason=REJECT_REASON)` и снятие отсрочки «Позже» (`set_snooze(post_id, None)`), чтобы отменённый пост не всплыл в resend_due. StaleStatusError (параллельное решение другого админа) -> already_done с перечитанным статусом. Лог `review.cancelled` с news_id и by.
3. **`cancel_all_reviews(by: int, deps, batch_size: int) -> int`** — отменяет все посты в pending_review пачками: `claim_batch(PENDING_REVIEW, batch_size)` (SKIP LOCKED — пост, который в эту секунду одобряет другой админ, пропускается), в той же транзакции set_status и снятие отсрочки для каждого; повторять, пока пачка не пустая. Возвращает число отменённых. Лог `review.cancelled_all` с количеством и by. batch_size — pipeline.batch_size.
4. **Модерация** (handlers/moderation.py): ветка «Отклонить» в on_review вызывает cancel_review вместо прямого set_status; тексты и поведение для админа не меняются («Уже обработано: {статус}» при already_done). ModerationDeps получает необязательное поле `review: ReviewDeps | None = None`; если None — прежний код (так существующие тесты модерации не меняются). В app/main.py передай ReviewDeps.

Машина состояний не меняется: переход pending_review -> rejected уже есть.

**Критерий готовности:** Пост на модерации можно отменить одной функцией: он получает rejected, больше не приходит на модерацию и не публикуется; двойная отмена и гонка с одобрением безопасны.

**Граничные случаи:** Отмена уже одобренного (approved) или опубликованного поста -> already_done, статус не меняется; несуществующий id -> not_found; пост был отложен «Позже» -> после отмены resend_due его не присылает; два админа отменяют одновременно -> один cancelled, второй already_done; cancel_all при пустой модерации -> 0; cancel_all, пока другой админ одобряет пост, -> этот пост пропущен и остаётся одобренным.

**Тесты:** `tests/unit/test_review_cancel.py` — все исходы cancel_review и cancel_all_reviews на fakes; `tests/integration/test_review_cancel_db.py` (маркер integration) — реальная БД: отмена, снятие отсрочки (list_due_reviews после отмены пуст), cancel_all с параллельно заблокированной строкой (вторая сессия держит claim_one на посте) -> пост пропущен; `tests/unit/test_bot_moderation.py` — «Отклонить» через ReviewDeps даёт тот же текст, что и раньше.

**Контекст проекта:** Python 3.11+, SQLAlchemy 2 (async). Кнопки в /queue — шаг 115.

**Файлы:** app/pipeline/review.py, app/bot/handlers/moderation.py, app/main.py, tests/unit/test_review_cancel.py, tests/integration/test_review_cancel_db.py, tests/unit/test_bot_moderation.py.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Токены и ключи — SecretStr (не попадают в repr и логи). Смена статуса новости — только через set_status и app/domain/status.py. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Если меняется состав или порядок компонентов в app/main.py — обнови ожидания tests/unit/test_app_wiring.py; новая секция конфигурации должна быть описана в README.md (это проверяет tests/unit/test_readme.py). Новых ключей конфигурации не добавляй.

**Формат ответа:** Полные файлы из списка «Файлы». Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(pipeline): отмена постов на модерации"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
