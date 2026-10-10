# Промпты для пошаговой разработки бота новостей

Всего шагов: 70. Один шаг — одна атомарная задача (функция, модуль или тест) и ровно один коммит.

## Как работать

1. Скопируйте `CLAUDE.md` и папку `prompts/` в корень пустого репозитория, сделайте первый коммит `chore: промпты и автопилот`.
2. Узнайте следующий шаг: `python prompts/next_step.py`.
3. Откройте репозиторий в Claude Code и напишите: «Выполни @prompts/<файл шага>».
4. Проверьте результат и коммит, затем отметьте шаг: `python prompts/next_step.py --done`.

Автоматический режим: напишите в Claude Code «Запусти автопилот» — он выполнит шаги по очереди с коммитом и `git push` после каждого (правила — в разделе «Автопилот» файла `CLAUDE.md`).

`next_step.py --done` сверяет сообщение последнего коммита с коммитом шага и проверяет, что рабочее дерево чистое. Прогресс хранится в `.git/next_step_progress.json`, поэтому рабочее дерево остаётся чистым.

## Этапы

- Этап 1 — Каркас проекта
- Этап 2 — База данных
- Этап 3 — Сборщики: RSS и сайты
- Этап 4 — Сборщик Telegram-каналов
- Этап 5 — Нормализация, фильтры и дедупликация
- Этап 6 — Обработка через DeepSeek
- Этап 7 — Публикатор в канал
- Этап 8 — Бот управления, модерация и сборка приложения
- Этап 9 — Тестирование
- Этап 10 — Деплой, мониторинг, документация

## Значения, которые нужно задать до соответствующих шагов

Эти ключи `configs/base.yaml` не выдумываются исполнителем: если их нет, шаг остановится и перечислит пробел.

- шаг 003: app.timezone — задаёт человек (IANA-имя, например Europe/Moscow)
- шаг 018: collectors.user_agent — задаёт человек (имя бота и контакт)
- шаг 033: filters.allowed_languages — задаёт человек
- шаг 038: ai.model — задаёт человек (сверить с документацией DeepSeek)
- шаг 039: channel.topic, channel.language, channel.tone, channel.max_title_chars, channel.max_summary_chars, channel.hashtags_max — задаёт человек
- шаг 042: ai.price_input_per_1m, ai.price_output_per_1m, ai.daily_budget — задаёт человек
- шаг 043: ai.min_score — задаёт человек
- шаг 048: publisher.quiet_hours — задаёт человек (например [`23:00`, `07:00`] или пусто)

Кроме того, человек сам: получает токены и ключи (BotFather, my.telegram.org, DeepSeek), создаёт канал и делает бота админом, выполняет вход Telethon через `scripts/telethon_login.py`, собирает размеченный набор из 200 новостей для `scripts/eval_dedup.py` и проводит тестовый прогон 3–5 дней.

## Список шагов

| Шаг | Задача | Этап | Результат | Файл |
| --- | --- | --- | --- | --- |
| 001 | P001 | 1 | pyproject.toml + каркас пакета app/ + Makefile + pre-commit + .gitignore | [001_P001_karkas-proekta-pyproject-i-makefile.md](001_P001_karkas-proekta-pyproject-i-makefile.md) |
| 002 | P001 | 1 | CLAUDE.md с инвариантами + README-каркас | [002_P001_claude-md-s-invariantami-proekta-i-readme.md](002_P001_claude-md-s-invariantami-proekta-i-readme.md) |
| 003 | P002 | 1 | app/config.py + .env.example + configs/base.yaml | [003_P002_settings-na-pydantic-settings-i-env-example.md](003_P002_settings-na-pydantic-settings-i-env-example.md) |
| 004 | P003 | 1 | app/logging.py: JSON-логи с контекстом и маскировкой секретов | [004_P003_json-logirovanie-structlog-s-kontekstom.md](004_P003_json-logirovanie-structlog-s-kontekstom.md) |
| 005 | P004 | 1 | app/main.py + app/__main__.py: запуск и корректная остановка | [005_P004_tochka-vhoda-s-zhiznennym-tsiklom-komponentov.md](005_P004_tochka-vhoda-s-zhiznennym-tsiklom-komponentov.md) |
| 006 | P004 | 1 | docker/docker-compose.dev.yml с PostgreSQL 16 + цели Makefile | [006_P004_docker-compose-s-postgresql-dlya-razrabotki.md](006_P004_docker-compose-s-postgresql-dlya-razrabotki.md) |
| 007 | P005 | 2 | app/db/base.py: движок, фабрика сессий, Base | [007_P005_asinhronnyy-dvizhok-i-fabrika-sessiy.md](007_P005_asinhronnyy-dvizhok-i-fabrika-sessiy.md) |
| 008 | P005 | 2 | app/domain/enums.py + модели Source и NewsItem | [008_P005_modeli-source-i-newsitem-i-perechisleniya.md](008_P005_modeli-source-i-newsitem-i-perechisleniya.md) |
| 009 | P005 | 2 | Модели ProcessedPost, Publication, Keyword, Admin, Setting | [009_P005_modeli-obrabotannyh-postov-publikatsiy-i-nastroek.md](009_P005_modeli-obrabotannyh-postov-publikatsiy-i-nastroek.md) |
| 010 | P006 | 2 | Alembic (async) + миграция 0001 со всеми таблицами и индексами | [010_P006_alembic-i-pervaya-migratsiya-s-indeksami.md](010_P006_alembic-i-pervaya-migratsiya-s-indeksami.md) |
| 011 | P007 | 2 | SourceRepository | [011_P007_repozitoriy-istochnikov.md](011_P007_repozitoriy-istochnikov.md) |
| 012 | P007 | 2 | NewsRepository: вставка без дублей и поиск | [012_P007_repozitoriy-novostey-vstavka-bez-dubley.md](012_P007_repozitoriy-novostey-vstavka-bez-dubley.md) |
| 013 | P007 | 2 | app/domain/status.py: машина состояний новости | [013_P007_mashina-sostoyaniy-statusov-novosti.md](013_P007_mashina-sostoyaniy-statusov-novosti.md) |
| 014 | P007 | 2 | NewsRepository: смена статуса и захват пачки | [014_P007_smena-statusa-i-zahvat-pachki-novostey.md](014_P007_smena-statusa-i-zahvat-pachki-novostey.md) |
| 015 | P007 | 2 | Репозитории ProcessedPost и Publication | [015_P007_repozitorii-obrabotannyh-postov-i-publikatsiy.md](015_P007_repozitorii-obrabotannyh-postov-i-publikatsiy.md) |
| 016 | P007 | 2 | Репозитории Keyword, Admin, Setting | [016_P007_repozitorii-klyuchevyh-slov-adminov-i-nastroek.md](016_P007_repozitorii-klyuchevyh-slov-adminov-i-nastroek.md) |
| 017 | P008 | 3 | app/collectors/base.py: RawItem, BaseCollector, ошибки | [017_P008_bazovyy-interfeys-sborschika.md](017_P008_bazovyy-interfeys-sborschika.md) |
| 018 | P008 | 3 | app/collectors/http.py: HTTP-клиент с лимитами и повторами | [018_P008_http-klient-s-taymautami-limitom-i-povtorami.md](018_P008_http-klient-s-taymautami-limitom-i-povtorami.md) |
| 019 | P008 | 3 | app/collectors/robots.py: проверка robots.txt с кэшем | [019_P008_proverka-robots-txt-s-keshem.md](019_P008_proverka-robots-txt-s-keshem.md) |
| 020 | P009 | 3 | app/collectors/rss.py: RSS/Atom-сборщик | [020_P009_rss-sborschik-s-etag-i-last-modified.md](020_P009_rss-sborschik-s-etag-i-last-modified.md) |
| 021 | P010 | 3 | app/collectors/web_list.py: ссылки на статьи по селекторам | [021_P010_izvlechenie-ssylok-na-stati-po-selektoram.md](021_P010_izvlechenie-ssylok-na-stati-po-selektoram.md) |
| 022 | P010 | 3 | app/collectors/web_article.py: текст, картинка и дата статьи | [022_P010_razbor-stati-cherez-trafilatura.md](022_P010_razbor-stati-cherez-trafilatura.md) |
| 023 | P010 | 3 | app/collectors/web.py: WebCollector | [023_P010_sborschik-saytov-bez-rss.md](023_P010_sborschik-saytov-bez-rss.md) |
| 024 | P011 | 3 | app/collectors/health.py + app/domain/alerts.py | [024_P011_otslezhivanie-zdorovya-istochnikov.md](024_P011_otslezhivanie-zdorovya-istochnikov.md) |
| 025 | P011 | 3 | app/collectors/runner.py: цикл сбора по расписанию | [025_P011_tsikl-sbora-iz-rss-i-saytov.md](025_P011_tsikl-sbora-iz-rss-i-saytov.md) |
| 026 | P012 | 4 | Telethon-клиент, вход по сессии и обработка FloodWait | [026_P012_klient-telethon-i-obrabotka-floodwait.md](026_P012_klient-telethon-i-obrabotka-floodwait.md) |
| 027 | P013 | 4 | app/collectors/telegram/convert.py: сообщение -> RawItem | [027_P013_preobrazovanie-soobscheniya-kanala-v-novost.md](027_P013_preobrazovanie-soobscheniya-kanala-v-novost.md) |
| 028 | P013 | 4 | app/collectors/telegram/albums.py: склейка альбомов | [028_P013_skleyka-albomov-po-grouped-id.md](028_P013_skleyka-albomov-po-grouped-id.md) |
| 029 | P013 | 4 | app/collectors/telegram/ads.py: отсев рекламы и репостов | [029_P013_filtr-reklamy-i-pereslannyh-soobscheniy.md](029_P013_filtr-reklamy-i-pereslannyh-soobscheniy.md) |
| 030 | P014 | 4 | app/collectors/telegram/collector.py: подписка и догрузка | [030_P014_sborschik-kanalov-s-podpiskoy-i-dogruzkoy.md](030_P014_sborschik-kanalov-s-podpiskoy-i-dogruzkoy.md) |
| 031 | P015 | 5 | app/pipeline/normalizer.py: clean_html и canonical_url | [031_P015_ochistka-html-i-kanonicheskiy-url.md](031_P015_ochistka-html-i-kanonicheskiy-url.md) |
| 032 | P015 | 5 | Язык, content_hash и normalize_item | [032_P015_opredelenie-yazyka-i-hesh-soderzhimogo.md](032_P015_opredelenie-yazyka-i-hesh-soderzhimogo.md) |
| 033 | P016 | 5 | app/pipeline/filters.py: фильтры новостей | [033_P016_filtry-vozrasta-yazyka-i-klyuchevyh-slov.md](033_P016_filtry-vozrasta-yazyka-i-klyuchevyh-slov.md) |
| 034 | P017 | 5 | SimHash и расстояние Хэмминга | [034_P017_simhash-zagolovka-i-pervogo-abzatsa.md](034_P017_simhash-zagolovka-i-pervogo-abzatsa.md) |
| 035 | P017 | 5 | Deduplicator: URL, хеш, SimHash и кластеры | [035_P017_trehurovnevaya-deduplikatsiya.md](035_P017_trehurovnevaya-deduplikatsiya.md) |
| 036 | P017 | 5 | scripts/eval_dedup.py: подбор порога SimHash | [036_P017_otsenka-poroga-deduplikatsii-na-razmechennyh-dannyh.md](036_P017_otsenka-poroga-deduplikatsii-na-razmechennyh-dannyh.md) |
| 037 | P017 | 5 | app/pipeline/stage_normalize.py: стадия нормализации | [037_P017_stadiya-normalizatsii-filtrov-i-deduplikatsii.md](037_P017_stadiya-normalizatsii-filtrov-i-deduplikatsii.md) |
| 038 | P018 | 6 | app/pipeline/ai/client.py: клиент DeepSeek | [038_P018_klient-deepseek-s-semaforom-i-povtorami.md](038_P018_klient-deepseek-s-semaforom-i-povtorami.md) |
| 039 | P019 | 6 | app/pipeline/ai/prompts.py: версионируемый промпт | [039_P019_versioniruemye-prompty-pereskaza.md](039_P019_versioniruemye-prompty-pereskaza.md) |
| 040 | P020 | 6 | app/pipeline/ai/schema.py: модель и разбор ответа | [040_P020_shema-i-razbor-json-otveta-modeli.md](040_P020_shema-i-razbor-json-otveta-modeli.md) |
| 041 | P021 | 6 | app/pipeline/ai/factcheck.py: сверка цифр и имён | [041_P021_proverka-tsifr-i-imen-v-pereskaze.md](041_P021_proverka-tsifr-i-imen-v-pereskaze.md) |
| 042 | P022 | 6 | app/pipeline/ai/budget.py: стоимость и дневной лимит | [042_P022_uchet-stoimosti-i-dnevnoy-byudzhet.md](042_P022_uchet-stoimosti-i-dnevnoy-byudzhet.md) |
| 043 | P023 | 6 | app/pipeline/ai/processor.py: обработка одной новости | [043_P023_protsessor-novosti-cherez-deepseek.md](043_P023_protsessor-novosti-cherez-deepseek.md) |
| 044 | P023 | 6 | app/pipeline/stage_ai.py: стадия ИИ-обработки | [044_P023_stadiya-obrabotki-cherez-deepseek.md](044_P023_stadiya-obrabotki-cherez-deepseek.md) |
| 045 | P024 | 7 | app/publisher/formatter.py: HTML-разметка поста | [045_P024_formatirovanie-posta-v-html.md](045_P024_formatirovanie-posta-v-html.md) |
| 046 | P024 | 7 | app/publisher/limits.py: подгонка под лимиты Telegram | [046_P024_podgonka-posta-pod-limity-dliny.md](046_P024_podgonka-posta-pod-limity-dliny.md) |
| 047 | P025 | 7 | app/publisher/media.py: проверка картинки | [047_P025_proverka-kartinki-pered-otpravkoy.md](047_P025_proverka-kartinki-pered-otpravkoy.md) |
| 048 | P026 | 7 | app/publisher/slots.py: расчёт времени публикации | [048_P026_raschet-slota-publikatsii-s-tihimi-chasami.md](048_P026_raschet-slota-publikatsii-s-tihimi-chasami.md) |
| 049 | P026 | 7 | app/publisher/publisher.py: отправка в канал | [049_P026_otpravka-postov-v-kanal-cherez-aiogram.md](049_P026_otpravka-postov-v-kanal-cherez-aiogram.md) |
| 050 | P026 | 7 | app/publisher/stage_publish.py: стадия публикации | [050_P026_stadiya-publikatsii-po-raspisaniyu.md](050_P026_stadiya-publikatsii-po-raspisaniyu.md) |
| 051 | P027 | 8 | app/bot/middlewares.py: доступ только для админов | [051_P027_middleware-dostupa-tolko-dlya-adminov.md](051_P027_middleware-dostupa-tolko-dlya-adminov.md) |
| 052 | P028 | 8 | Фабрика бота и диспетчера, /start и /help | [052_P028_fabrika-dispetchera-i-komandy-start-i-help.md](052_P028_fabrika-dispetchera-i-komandy-start-i-help.md) |
| 053 | P028 | 8 | /status, /pause, /resume | [053_P028_komandy-status-pause-i-resume.md](053_P028_komandy-status-pause-i-resume.md) |
| 054 | P029 | 8 | Список источников и действия кнопками | [054_P029_spisok-istochnikov-s-inline-knopkami.md](054_P029_spisok-istochnikov-s-inline-knopkami.md) |
| 055 | P029 | 8 | Диалог добавления источника (FSM) | [055_P029_dialog-dobavleniya-istochnika.md](055_P029_dialog-dobavleniya-istochnika.md) |
| 056 | P030 | 8 | Ключевые и стоп-слова через бота | [056_P030_upravlenie-klyuchevymi-i-stop-slovami.md](056_P030_upravlenie-klyuchevymi-i-stop-slovami.md) |
| 057 | P031 | 8 | Отправка черновиков на модерацию | [057_P031_otpravka-chernovikov-adminam-na-moderatsiyu.md](057_P031_otpravka-chernovikov-adminam-na-moderatsiyu.md) |
| 058 | P031 | 8 | Обработка решений модератора | [058_P031_obrabotka-knopok-moderatsii.md](058_P031_obrabotka-knopok-moderatsii.md) |
| 059 | P032 | 8 | app/alerts.py: AlertService | [059_P032_otpravka-alertov-adminam-s-podavleniem-povtorov.md](059_P032_otpravka-alertov-adminam-s-podavleniem-povtorov.md) |
| 060 | P033 | 8 | app/scheduler.py: регистрация задач | [060_P033_planirovschik-zadach-apscheduler.md](060_P033_planirovschik-zadach-apscheduler.md) |
| 061 | P033 | 8 | Сборка приложения в app/main.py | [061_P033_sborka-komponentov-prilozheniya.md](061_P033_sborka-komponentov-prilozheniya.md) |
| 062 | P034 | 9 | Интеграционные тесты всего пайплайна | [062_P034_skvoznoy-test-payplayna.md](062_P034_skvoznoy-test-payplayna.md) |
| 063 | P035 | 9 | Порог покрытия 70% для ключевых модулей | [063_P035_porog-pokrytiya-dlya-klyuchevyh-moduley.md](063_P035_porog-pokrytiya-dlya-klyuchevyh-moduley.md) |
| 064 | P036 | 9 | scripts/run_report.py: метрики тестового прогона | [064_P036_otchet-po-metrikam-testovogo-progona.md](064_P036_otchet-po-metrikam-testovogo-progona.md) |
| 065 | P037 | 10 | Dockerfile (multi-stage) + .dockerignore | [065_P037_mnogostupenchatyy-dockerfile.md](065_P037_mnogostupenchatyy-dockerfile.md) |
| 066 | P037 | 10 | docker/docker-compose.prod.yml + entrypoint с миграциями | [066_P037_boevoy-docker-compose-i-entrypoint.md](066_P037_boevoy-docker-compose-i-entrypoint.md) |
| 067 | P038 | 10 | app/health.py: проверка живости | [067_P038_proverka-zhivosti-po-heartbeat-i-bd.md](067_P038_proverka-zhivosti-po-heartbeat-i-bd.md) |
| 068 | P039 | 10 | scripts/backup.py: бэкап БД и сессии | [068_P039_bekap-bd-i-telethon-sessii-s-rotatsiey.md](068_P039_bekap-bd-i-telethon-sessii-s-rotatsiey.md) |
| 069 | P040 | 10 | .github/workflows/ci.yml | [069_P040_lintery-testy-i-sborka-obraza-v-github-actions.md](069_P040_lintery-testy-i-sborka-obraza-v-github-actions.md) |
| 070 | P041 | 10 | README.md: полная документация | [070_P041_polnaya-dokumentatsiya-po-ustanovke-i-ekspluatatsii.md](070_P041_polnaya-dokumentatsiya-po-ustanovke-i-ekspluatatsii.md) |
