# Шаг 088 из 088 · P050 · Этап 13 — Ежедневный отчёт в боте управления

**Задача трекера:** Ежедневный отчёт: доставка в бот
**Результат:** Отчёт за день сам приходит всем админам в daily_report.send_at; /report в меню; README
**Коммит:** `feat(app): ежедневный отчёт администраторам`
**Как запустить:** откройте репозиторий в Claude Code и напишите: «Выполни @prompts/088_P050_ezhednevnaya-otpravka-otcheta.md». После коммита: `python prompts/next_step.py --done`.

## Задача

1. **Отправка** — `DailyReportSender` в app/reports/sender.py:
   - `async send_due(now: datetime) -> bool`: если daily_report.enabled = false -> False; если в настройке `DAILY_REPORT_SETTING = "daily_report_last"` уже записана сегодняшняя дата (в tz) -> False; если текущее время в tz раньше send_at -> False. Иначе строит отчёт за `today` (build_report из шага 087), отправляет каждому админу все сообщения подряд (под последним — report_keyboard), записывает сегодняшнюю дату и возвращает True.
   - Дата записывается после отправки хотя бы одному админу; ошибка отправки одному админу логируется и не мешает остальным; если не удалось никому — дата не пишется, и следующий запуск повторит отправку.
   - Защита от двойной отправки при двух экземплярах приложения: чтение и запись даты в одной транзакции с блокировкой строки настройки (SELECT ... FOR UPDATE через SettingsRepository; если такого метода нет — добавь `get_for_update` в SettingsRepository с интеграционным тестом).
2. **Планировщик:** задача `daily_report` с интервалом scheduler.daily_report_every_min (по умолчанию 5 минут) вызывает send_due. Так отчёт уходит в send_at, а если приложение было выключено в это время, — при первом запуске после send_at в тот же день. Пропущенный вчерашний отчёт не досылается (его можно запросить /report -> «Вчера»). Сбой задачи — как у остальных (job_failed).
3. **app/main.py:** ReportDeps (session_scope, DailyContext из settings: timezone, ai.daily_budget, publisher.score_decay_per_h, daily_report.top_posts; metrics; publisher.text_limit), DailyReportSender (bot, admin_ids, SettingsRepository), регистрация задачи, `report_handlers.create_router()` и `"report_deps"` в workflow_data.
4. **texts.COMMANDS:** после "errors" добавь `"report": "отчёт за день, вчера или неделю"`.
5. **README.md:** раздел «Ежедневный отчёт»: когда приходит и как изменить время (DAILY_REPORT__SEND_AT=23:30 или configs), что в каждом блоке и как считаются воронка и сравнение со вчера, как отключить, команда /report, связь со scripts/run_report.py (одни и те же метрики).
6. **Конфигурация:** daily_report.enabled = true, daily_report.send_at = "23:30" (по умолчанию после начала тихих часов, когда публикаций за день уже не будет; формат ЧЧ:ММ проверяется валидатором как quiet_hours), scheduler.daily_report_every_min = 5.

**Критерий готовности:** Каждый день в 23:30 по app.timezone все админы получают отчёт за день ровно один раз — в том числе после перезапуска; /report есть в меню; make lint, make test и make test-integration проходят.

**Граничные случаи:** Перезапуск в 23:40 после отправки -> повторно не отправляется; приложение запущено в 23:45, а в 23:30 не работало -> отчёт уходит при первом запуске задачи; запуск в 00:10 -> вчерашний не досылается, сегодняшний — в 23:30; два экземпляра приложения -> один отчёт; enabled = false -> ничего не отправляется, /report работает; send_at = "25:00" -> ошибка конфигурации при запуске; не-админ не может вызвать /report и нажать кнопки rp:... .

**Тесты:** `tests/unit/test_report_sender.py` — все граничные случаи с fake-часами, fake-ботом и fake-настройками; ошибка у одного админа; никому не отправлено -> дата не записана. `tests/integration/test_daily_report_send.py` (маркер integration) — два параллельных send_due с реальной БД отправляют отчёт один раз. `tests/unit/test_scheduler.py` — регистрация задачи daily_report. `tests/unit/test_bot_middleware.py` — отказ не-админу для /report и rp:.... `tests/unit/test_config.py` — новые ключи и неверный send_at.

**Контекст проекта:** Python 3.11+, aiogram 3.x, APScheduler, PostgreSQL 16. Последний шаг этапа 13.

**Файлы:** app/reports/sender.py, app/scheduler.py, app/main.py, app/bot/texts.py, app/db/repositories/misc.py (если нужен get_for_update), configs/base.yaml, app/config.py, README.md, тестовые файлы из списка выше.

**Стиль проекта:** ruff, mypy strict, docstrings Google Style; инварианты — из CLAUDE.md.

**Ограничения:** Ни один модуль, кроме app/config.py, не читает os.environ. Новые зависимости, заглушки (TODO, «...») и молчаливый except Exception: pass запрещены. Существующие тесты не ослабляй. Новые ключи конфигурации добавь в configs/base.yaml и в типизированные секции в app/config.py; ключи, помеченные «задаёт человек», не заполняй сам — если их нет в configs/base.yaml, перечисли пробел и остановись. Ключи этого шага: daily_report.enabled = true, daily_report.send_at = "23:30", scheduler.daily_report_every_min = 5. Раздел «Автопилот» в CLAUDE.md и файлы prompts/ не меняй.

**Формат ответа:** Полные файлы: app/reports/sender.py, app/scheduler.py, app/main.py, app/bot/texts.py, configs/base.yaml, app/config.py, изменённые репозитории и тестовые файлы; для README.md — новый раздел целиком. Тесты обязательны; проверка: `make lint`, `make test`, `make test-integration`, `python -c "import app.main"`. Комментарии — только docstrings Google Style для сложных функций. После кода — таблица [Тест] -> [Критерий]. При нехватке данных сначала перечисли конкретные пробелы, значения не выдумывай (пороги и параметры — из configs/).

## Порядок работы (для ИИ-исполнителя)

1. Перед началом выполни `git status`. Если есть незакоммиченные изменения — остановись и сообщи об этом, ничего не меняй.
2. Выполни задачу выше полностью. Соблюдай CLAUDE.md и ограничения из задачи. Если данных не хватает — перечисли пробелы и остановись, не выдумывай значения.
3. Прогони проверки: `make lint`, `make test` и команды проверки из «Формата ответа». Всё должно пройти; если что-то падает — исправь в рамках этой задачи.
4. Сделай ровно один коммит:

```
git add -A
git commit -m "feat(app): ежедневный отчёт администраторам"
```

5. Остановись. Не переходи к следующему шагу. В конце выведи таблицу [Тест] -> [Критерий] и список того, что осталось сделать человеку (если есть).
