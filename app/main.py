"""Точка входа: жизненный цикл компонентов и остановка по сигналам."""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from typing import Protocol
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts import AlertService
from app.bot import BotDeps, create_bot, create_dispatcher
from app.bot.handlers import errors as errors_handlers
from app.bot.handlers import keywords as keywords_handlers
from app.bot.handlers import moderation as moderation_handlers
from app.bot.handlers import queue as queue_handlers
from app.bot.handlers import report as report_handlers
from app.bot.handlers import source_add as source_add_handlers
from app.bot.handlers import sources as sources_handlers
from app.bot.handlers import status as status_handlers
from app.bot.handlers.common import setup_bot_commands
from app.bot.handlers.errors import MUTED_KINDS_SETTING, ErrorsDeps
from app.bot.handlers.keywords import KeywordsDeps
from app.bot.handlers.moderation import ModerationDeps
from app.bot.handlers.queue import QueueDeps
from app.bot.handlers.source_add import SourceAddDeps
from app.bot.handlers.sources import SourcesDeps
from app.bot.handlers.status import StatusDeps
from app.bot.moderation import BotReviewNotifier, ReviewNotifierDeps
from app.bot.preview import PreviewDeps
from app.bot.screen import ScreenRequestMiddleware, ScreenTracker, ScreenUpdateMiddleware
from app.collectors.base import BaseCollector
from app.collectors.health import SourceHealthTracker
from app.collectors.http import HttpClient
from app.collectors.robots import RobotsChecker
from app.collectors.rss import RssCollector
from app.collectors.runner import CollectionDeps, run_collection_cycle
from app.collectors.telegram.client import TelegramAuthRequired, build_client
from app.collectors.telegram.collector import TelegramCollector, TelegramDeps
from app.collectors.web import WebCollector
from app.components import (
    BotPollingComponent,
    ClosingComponent,
    DatabaseComponent,
    SchedulerComponent,
)
from app.config import Settings, get_settings
from app.db.base import create_engine, create_session_factory, session_scope
from app.db.models import Incident
from app.db.repositories.ai_requests import AiRequestRepository
from app.db.repositories.incidents import IncidentRepository
from app.db.repositories.misc import AdminRepository, KeywordRepository, SettingsRepository
from app.db.repositories.news import NewsRepository, Priority
from app.db.repositories.posts import ProcessedPostRepository, PublicationRepository
from app.db.repositories.screens import ScreenRepository
from app.db.repositories.sources import SourceRepository
from app.domain.alerts import AlertEvent, AlertKind
from app.domain.enums import SourceType
from app.health import HEARTBEAT_SETTING, heartbeat_value
from app.logging import setup_logging
from app.pipeline.ai.budget import BudgetGuard
from app.pipeline.ai.client import DeepSeekClient
from app.pipeline.ai.processor import AiProcessor, AiRepos
from app.pipeline.queue_limit import QueueAdmission, QueueLimitRepos, trim_queue
from app.pipeline.retry import RetryDeps
from app.pipeline.stage_ai import AiStageDeps, process_normalized_batch
from app.pipeline.stage_normalize import NormalizeDeps, process_new_batch
from app.publisher.media import ImageCheck, check_image
from app.publisher.publisher import Publisher
from app.publisher.stage_publish import (
    PAUSED_SETTING,
    ManualPublishDeps,
    PublishDeps,
    publish_due,
    recover_stuck,
)
from app.reports.daily import DailyContext
from app.reports.sender import DailyReportSender
from app.reports.service import ReportDeps
from app.scheduler import SchedulerDeps, build_scheduler
from app.stats import StatsDeps, collect_post_stats

log = structlog.get_logger(__name__)

STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class Component(Protocol):
    """Компонент приложения с асинхронным запуском и остановкой."""

    @property
    def name(self) -> str:
        """Имя компонента для логов."""
        raise NotImplementedError

    async def start(self) -> None:
        """Запускает компонент."""

    async def stop(self) -> None:
        """Останавливает компонент."""


class Application:
    """Запускает компоненты по порядку и останавливает в обратном порядке."""

    def __init__(self, components: Sequence[Component]) -> None:
        self._components = list(components)
        self._started: list[Component] = []

    @property
    def components(self) -> list[Component]:
        """Компоненты в порядке запуска."""
        return list(self._components)

    async def start(self) -> None:
        """Запускает все компоненты.

        Если start() компонента упал, уже запущенные компоненты останавливаются,
        а исключение пробрасывается дальше.
        """
        for component in self._components:
            try:
                await component.start()
            except Exception:
                log.exception("component.start_failed", component=component.name)
                await self.stop()
                raise
            self._started.append(component)
            log.info("component.started", component=component.name)

    async def stop(self) -> None:
        """Останавливает запущенные компоненты в обратном порядке.

        Ошибка в stop() одного компонента логируется и не мешает остановить остальные.
        """
        while self._started:
            component = self._started.pop()
            try:
                await component.stop()
            except Exception:
                log.exception("component.stop_failed", component=component.name)
            else:
                log.info("component.stopped", component=component.name)

    async def serve(self, stop_event: asyncio.Event) -> None:
        """Запускает компоненты, ждёт stop_event и останавливает их.

        Args:
            stop_event: Событие, по которому начинается остановка.
        """
        await self.start()
        log.info("app.started", components=[c.name for c in self._components])
        try:
            await stop_event.wait()
        finally:
            await self.stop()
            log.info("app.stopped")


def build_components(settings: Settings) -> list[Component]:
    """Создаёт и связывает компоненты приложения.

    Порядок запуска: БД -> HTTP -> клиент модели -> бот -> Telegram-сборщик ->
    планировщик; остановка — в обратном порядке. Вызывается внутри работающего
    цикла событий, ни один компонент не создаёт свой.

    Args:
        settings: Настройки приложения.

    Returns:
        Компоненты в порядке запуска.
    """
    started_at = datetime.now(UTC)
    engine = create_engine(settings)
    factory = create_session_factory(engine)

    def scope() -> AbstractAsyncContextManager[AsyncSession]:
        return session_scope(factory)

    http = HttpClient(settings.collectors)
    robots = RobotsChecker(
        http, settings.collectors.user_agent, timedelta(hours=settings.collectors.robots_ttl_h)
    )
    bot = create_bot(settings)
    # Этот же бот публикует в канал, поэтому middleware исключает канал из экранов.
    bot.session.middleware(
        ScreenRequestMiddleware(
            ScreenTracker(scope, ScreenRepository, bot, settings.ui, lambda: datetime.now(UTC)),
            settings.telegram_channel_id,
            settings.ui,
        )
    )
    admins = _Admins(settings, scope)
    silent_kinds = frozenset(settings.alerts.silent_kinds)

    async def muted_kinds() -> set[AlertKind]:
        """Виды алертов, заглушённые через /errors (неизвестные значения пропускаются)."""
        async with scope() as session:
            stored = await SettingsRepository(session).get(MUTED_KINDS_SETTING, [])
        kinds: set[AlertKind] = set()
        for value in stored if isinstance(stored, list) else []:
            try:
                kinds.add(AlertKind(value))
            except ValueError:
                log.warning("alerts.unknown_muted_kind", kind=str(value))
        return kinds

    alerts = AlertService(
        bot,
        admins.ids,
        settings.alerts,
        store=_SessionIncidentStore(scope, settings.alerts.max_message_chars),
        muted=muted_kinds,
        silent_kinds=silent_kinds,
    )
    tz = ZoneInfo(settings.app.timezone)

    def health(session: AsyncSession) -> SourceHealthTracker:
        return SourceHealthTracker(SourceRepository(session), alerts, settings.collectors)

    async def is_known_url(url: str) -> bool:
        async with scope() as session:
            return await NewsRepository(session).exists_url(url)

    collectors: dict[SourceType, BaseCollector] = {
        SourceType.RSS: RssCollector(http),
        SourceType.WEB: WebCollector(http, robots, is_known_url, settings.collectors),
    }
    telegram = TelegramCollector(
        build_client(settings),
        TelegramDeps(
            session_scope=scope,
            source_repo=SourceRepository,
            news_repo=NewsRepository,
            health=health,
        ),
        settings.telegram,
        settings.filters.max_age_h,
    )
    deepseek = DeepSeekClient(settings, settings.ai)

    async def image_check(url: str | None) -> ImageCheck:
        return await check_image(url, http, settings.publisher)

    notifier = BotReviewNotifier(
        bot,
        ReviewNotifierDeps(
            session_scope=scope,
            news_repo=NewsRepository,
            source_repo=SourceRepository,
            post_repo=ProcessedPostRepository,
            admin_ids=admins.ids,
            check_image=image_check,
            cfg=settings.publisher,
        ),
    )
    budget = BudgetGuard(_SessionCost(scope), alerts, settings.ai, tz)

    def processor(repos: AiRepos) -> AiProcessor:
        return AiProcessor(deepseek, settings.channel, repos, settings.ai, budget=budget)

    collection_deps = CollectionDeps(
        session_scope=scope,
        source_repo=SourceRepository,
        news_repo=NewsRepository,
        health=health,
        collectors=collectors,
        max_parallel_sources=settings.collectors.max_parallel_sources,
    )
    normalize_deps = NormalizeDeps(
        session_scope=scope,
        news_repo=NewsRepository,
        keyword_repo=KeywordRepository,
        savepoint=lambda session: session.begin_nested(),
        normalize=settings.normalize,
        filters=settings.filters,
        dedup=settings.dedup,
        batch_size=settings.pipeline.batch_size,
    )
    admission = QueueAdmission(
        news_repo=NewsRepository,
        source_repo=SourceRepository,
        max_per_source=settings.publisher.max_queued_per_source,
        priority=Priority.from_config(settings.publisher),
    )
    ai_deps = AiStageDeps(
        session_scope=scope,
        news_repo=NewsRepository,
        source_repo=SourceRepository,
        post_repo=ProcessedPostRepository,
        request_repo=AiRequestRepository,
        settings_repo=SettingsRepository,
        processor=processor,
        budget=budget,
        notifier=notifier,
        sink=alerts,
        batch_size=settings.pipeline.batch_size,
        max_concurrency=settings.ai.max_concurrency,
        admission=admission,
    )
    publish_deps = PublishDeps(
        session_scope=scope,
        news_repo=NewsRepository,
        post_repo=ProcessedPostRepository,
        source_repo=SourceRepository,
        publication_repo=PublicationRepository,
        settings_repo=SettingsRepository,
        check_image=image_check,
        publisher=Publisher(bot, settings.telegram_channel_id, settings.publisher),
        sink=alerts,
        cfg=settings.publisher,
        channel_id=settings.telegram_channel_id,
        tz=tz,
    )
    manual_deps = ManualPublishDeps(publish=publish_deps, news_repo=NewsRepository)
    stats_deps = StatsDeps(
        session_scope=scope,
        publication_repo=PublicationRepository,
        fetcher=telegram,
        sink=alerts,
        cfg=settings.stats,
        channel_id=settings.telegram_channel_id,
    )
    preview_deps = PreviewDeps(
        session_scope=scope,
        news_repo=NewsRepository,
        source_repo=SourceRepository,
        check_image=image_check,
        cfg=settings.publisher,
    )

    async def is_paused(session: AsyncSession) -> bool:
        return bool(await SettingsRepository(session).get(PAUSED_SETTING, False))

    report_deps = ReportDeps(
        session_scope=scope,
        context=lambda: DailyContext(
            tz=tz,
            daily_budget=settings.ai.daily_budget,
            decay_per_h=settings.publisher.score_decay_per_h,
            top_posts=settings.daily_report.top_posts,
            channel_id=settings.telegram_channel_id,
        ),
        cfg=settings.daily_report,
        metrics=settings.metrics,
        text_limit=settings.publisher.text_limit,
        budget=settings.ai.daily_budget,
        tz=tz,
        clock=lambda: datetime.now(UTC),
        paused=is_paused,
    )
    report_sender = DailyReportSender(
        bot, admins.ids, SettingsRepository, report_deps, settings.daily_report
    )
    stale_tracker = SourceHealthTracker(_SessionSourceHealth(scope), alerts, settings.collectors)

    async def check_stale(now: datetime) -> int:
        async with scope() as session:
            sources = await SourceRepository(session).list_enabled()
        return len(await stale_tracker.check_stale(sources, now))

    async def purge_incidents(now: datetime) -> int:
        async with scope() as session:
            return await IncidentRepository(session).purge_resolved(
                now - timedelta(days=settings.alerts.retention_days)
            )

    async def retention(now: datetime) -> int:
        """Очищает тексты старых новостей пачками, не дольше retention.max_run_s.

        Каждая пачка — своя короткая транзакция. Если новостей больше, чем
        успевает один запуск, остальные очистит следующий.

        Args:
            now: Текущий момент.

        Returns:
            Сколько новостей очищено за этот запуск.
        """
        cfg = settings.retention
        before = now - timedelta(days=cfg.text_days)
        deadline = monotonic() + cfg.max_run_s
        stripped = 0
        while True:
            async with scope() as session:
                batch = await NewsRepository(session).strip_texts(before, cfg.batch_size)
            stripped += batch
            if batch < cfg.batch_size or monotonic() >= deadline:
                break
        log.info("retention.stripped", count=stripped, before=before.isoformat())
        return stripped

    async def heartbeat(now: datetime) -> None:
        async with scope() as session:
            await SettingsRepository(session).set(
                HEARTBEAT_SETTING, heartbeat_value(started_at, now)
            )

    scheduler, runner = build_scheduler(
        SchedulerDeps(
            collect=lambda now: run_collection_cycle(now, collection_deps),
            normalize=lambda now: process_new_batch(now, normalize_deps),
            ai=lambda now: process_normalized_batch(now, ai_deps),
            publish=lambda now: publish_due(now, publish_deps),
            check_stale=check_stale,
            resend_due=notifier.resend_due,
            heartbeat=heartbeat,
            purge_incidents=purge_incidents,
            daily_report=report_sender.send_due,
            recover_stuck=lambda now: recover_stuck(now, publish_deps),
            retention=retention,
            post_stats=lambda now: collect_post_stats(now, stats_deps),
            sink=alerts,
        ),
        settings.scheduler,
    )

    dispatcher = create_dispatcher(
        BotDeps(
            settings=settings,
            admin_repo=admins,
            screen_middleware=ScreenUpdateMiddleware(settings.ui),
            routers=[
                status_handlers.create_router(),
                sources_handlers.create_router(),
                source_add_handlers.create_router(),
                keywords_handlers.create_router(),
                moderation_handlers.create_router(),
                queue_handlers.create_router(),
                errors_handlers.create_router(),
                report_handlers.create_router(),
            ],
            workflow_data={
                "status_deps": StatusDeps(
                    session_scope=scope,
                    news_repo=NewsRepository,
                    source_repo=SourceRepository,
                    cost_repo=AiRequestRepository,
                    publication_repo=PublicationRepository,
                    settings_repo=SettingsRepository,
                    incident_repo=IncidentRepository,
                    max_queued_per_source=settings.publisher.max_queued_per_source,
                    daily_budget=settings.ai.daily_budget,
                    channel_id=settings.telegram_channel_id,
                    tz=tz,
                ),
                "sources_deps": SourcesDeps(
                    session_scope=scope,
                    source_repo=SourceRepository,
                    collectors=collectors,
                    reload_telegram=telegram.reload_sources,
                    page_size=settings.bot.page_size,
                ),
                "source_add_deps": SourceAddDeps(
                    session_scope=scope,
                    source_repo=SourceRepository,
                    collectors=collectors,
                    reload_telegram=telegram.reload_sources,
                    min_interval_min=settings.collectors.min_interval_min,
                ),
                "keywords_deps": KeywordsDeps(
                    session_scope=scope,
                    keyword_repo=KeywordRepository,
                    max_keyword_chars=settings.bot.max_keyword_chars,
                ),
                "moderation_deps": ModerationDeps(
                    session_scope=scope,
                    news_repo=NewsRepository,
                    post_repo=ProcessedPostRepository,
                    source_repo=SourceRepository,
                    cfg=settings.publisher,
                    snooze_min=settings.moderation.snooze_min,
                    admission=admission,
                ),
                "queue_deps": QueueDeps(
                    session_scope=scope,
                    news_repo=NewsRepository,
                    post_repo=ProcessedPostRepository,
                    publication_repo=PublicationRepository,
                    settings_repo=SettingsRepository,
                    source_repo=SourceRepository,
                    preview=preview_deps,
                    publisher_cfg=settings.publisher,
                    channel_id=settings.telegram_channel_id,
                    tz=tz,
                    page_size=settings.bot.page_size,
                    title_chars=settings.bot.queue_title_chars,
                    max_per_source=settings.publisher.max_queued_per_source,
                    clock=lambda: datetime.now(UTC),
                    manual=manual_deps,
                ),
                "errors_deps": ErrorsDeps(
                    session_scope=scope,
                    incident_repo=IncidentRepository,
                    news_repo=NewsRepository,
                    source_repo=SourceRepository,
                    settings_repo=SettingsRepository,
                    retry=RetryDeps(
                        session_scope=scope,
                        news_repo=NewsRepository,
                        post_repo=ProcessedPostRepository,
                        admission=admission,
                    ),
                    silent_kinds=silent_kinds,
                    page_size=settings.bot.page_size,
                    preview_chars=settings.bot.error_preview_chars,
                    text_limit=settings.publisher.text_limit,
                    tz=tz,
                    clock=lambda: datetime.now(UTC),
                ),
                "report_deps": report_deps,
            },
        )
    )
    return [
        DatabaseComponent(engine),
        StartMarkerComponent(scope, started_at),
        QueueTrimComponent(
            scope,
            settings.publisher.max_queued_per_source,
            Priority.from_config(settings.publisher),
        ),
        RecoverStuckComponent(publish_deps, lambda: datetime.now(UTC)),
        http,
        ClosingComponent("ai_client", deepseek.close),
        BotPollingComponent(bot, dispatcher, setup_bot_commands),
        telegram,
        SchedulerComponent(scheduler, runner),
    ]


class QueueTrimComponent:
    """Приводит очередь к лимиту источника при старте.

    Нужен, когда лимит включили или уменьшили, пока приложение не работало.
    Ошибка пробрасывается: стартовать с неизвестным состоянием очереди нельзя.
    """

    name = "queue_trim"

    def __init__(
        self,
        scope: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        max_per_source: int,
        priority: Priority,
    ) -> None:
        self._scope = scope
        self._max_per_source = max_per_source
        self._priority = priority

    async def start(self) -> None:
        """Вытесняет лишние посты каждого источника одной транзакцией."""
        try:
            async with self._scope() as session:
                repos = QueueLimitRepos(
                    news=NewsRepository(session), sources=SourceRepository(session)
                )
                await trim_queue(repos, self._max_per_source, self._priority)
        except Exception:
            log.exception("queue_limit.trim_failed")
            raise

    async def stop(self) -> None:
        """Ничего не делает."""


class RecoverStuckComponent:
    """Разбирает зависшие отправки при старте.

    Нужен, когда приложение упало между отправкой поста в канал и записью итога:
    такой пост остаётся в scheduled и сам из него не выйдет. Ошибка пробрасывается:
    стартовать, не разобравшись с возможным дублем в канале, нельзя.
    """

    name = "recover_stuck"

    def __init__(self, deps: PublishDeps, clock: Callable[[], datetime]) -> None:
        self._deps = deps
        self._clock = clock

    async def start(self) -> None:
        """Переводит зависшие посты в published или failed с publish_unknown."""
        try:
            await recover_stuck(self._clock(), self._deps)
        except Exception:
            log.exception("publish.recover_failed")
            raise

    async def stop(self) -> None:
        """Ничего не делает."""


class StartMarkerComponent:
    """Записывает время старта процесса в heartbeat (для startup_grace_s в app.health)."""

    name = "start_marker"

    def __init__(
        self,
        scope: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        started_at: datetime,
    ) -> None:
        self._scope = scope
        self._started_at = started_at

    async def start(self) -> None:
        """Сохраняет время старта; heartbeat ещё не было."""
        async with self._scope() as session:
            await SettingsRepository(session).set(
                HEARTBEAT_SETTING, heartbeat_value(self._started_at, None)
            )

    async def stop(self) -> None:
        """Ничего не делает: отметка старта не требует очистки."""


class _Admins:
    """Администраторы из настроек и таблицы admins (для алертов, модерации и middleware)."""

    def __init__(
        self, settings: Settings, scope: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._configured = list(settings.telegram_admin_ids)
        self._scope = scope

    async def ids(self) -> Sequence[int]:
        async with self._scope() as session:
            stored = [admin.telegram_id for admin in await AdminRepository(session).list()]
        return list(dict.fromkeys([*self._configured, *stored]))

    async def is_admin(self, telegram_id: int) -> bool:
        async with self._scope() as session:
            return await AdminRepository(session).is_admin(telegram_id)


class _SessionCost:
    """Расход на модель для BudgetGuard: каждый запрос — в своей сессии."""

    def __init__(self, scope: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self._scope = scope

    async def cost_since(self, dt: datetime) -> Decimal:
        async with self._scope() as session:
            return await AiRequestRepository(session).cost_since(dt)


class _SessionIncidentStore:
    """Журнал ошибок для AlertService: каждая запись — в своей транзакции."""

    def __init__(
        self,
        scope: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        max_message_chars: int,
    ) -> None:
        self._scope = scope
        self._max_message_chars = max_message_chars

    async def record(self, event: AlertEvent) -> Incident:
        async with self._scope() as session:
            return await IncidentRepository(session).record(event, self._max_message_chars)


class _SessionSourceHealth:
    """Репозиторий здоровья источников для долгоживущего трекера: каждый вызов — своя сессия."""

    def __init__(self, scope: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self._scope = scope

    async def register_error(self, source_id: int) -> int:
        async with self._scope() as session:
            return await SourceRepository(session).register_error(source_id)

    async def reset_errors(self, source_id: int) -> bool:
        async with self._scope() as session:
            return await SourceRepository(session).reset_errors(source_id)

    async def mark_item_seen(self, source_id: int, at: datetime) -> bool:
        async with self._scope() as session:
            return await SourceRepository(session).mark_item_seen(source_id, at)


async def run() -> None:
    """Загружает настройки, настраивает логи и работает до SIGINT/SIGTERM.

    Повторный сигнал во время остановки только повторно выставляет событие
    и не прерывает остановку.
    """
    settings = get_settings()
    setup_logging(settings.log_level, json_output=True)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in STOP_SIGNALS:
        loop.add_signal_handler(sig, stop_event.set)
    try:
        await Application(build_components(settings)).serve(stop_event)
    finally:
        for sig in STOP_SIGNALS:
            loop.remove_signal_handler(sig)


def main() -> None:
    """Запускает приложение; без сессии Telethon завершается с понятным сообщением."""
    try:
        asyncio.run(run())
    except TelegramAuthRequired as exc:
        sys.exit(f"Ошибка запуска: {exc}")
