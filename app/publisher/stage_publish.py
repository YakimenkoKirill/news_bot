"""Стадия публикации: один пост в разрешённый слот."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog

from app.config import PublisherConfig
from app.db.models import ProcessedPost, Publication
from app.domain.alerts import AlertEvent, AlertKind, AlertSink
from app.domain.enums import NewsStatus, PublicationStatus
from app.logging import bind_context
from app.publisher.formatter import PostNews, PostSource, build_post
from app.publisher.limits import FittedPost, fit_post
from app.publisher.media import ImageCheck
from app.publisher.publisher import PublishError
from app.publisher.slots import next_slot

log = structlog.get_logger(__name__)

PAUSED_SETTING = "paused"
PUBLISHABLE_STATUSES = (NewsStatus.APPROVED, NewsStatus.SCHEDULED)
MANUAL_STATUSES = (NewsStatus.APPROVED, NewsStatus.SCHEDULED, NewsStatus.PENDING_REVIEW)
REASON_PUBLISH_ERROR = "publish_error"
REASON_NO_POST = "no_processed_post"


class QueuedNews(PostNews, Protocol):
    """Новость в очереди публикации (NewsItem подходит структурно)."""

    id: int
    source_id: int
    status: NewsStatus


class PublishNewsRepo(Protocol):
    """Нужная стадии часть NewsRepository."""

    async def claim_next(
        self, statuses: Sequence[NewsStatus], decay_per_h: float
    ) -> QueuedNews | None:
        """Захватывает пост с наибольшим приоритетом (FOR UPDATE SKIP LOCKED)."""
        raise NotImplementedError

    async def set_status(
        self,
        news_id: int,
        expected: NewsStatus,
        new: NewsStatus,
        reason: str | None = None,
    ) -> None:
        """Смена статуса через машину состояний."""
        raise NotImplementedError


class ManualNewsRepo(PublishNewsRepo, Protocol):
    """Нужная ручной публикации часть NewsRepository."""

    async def claim_one(self, news_id: int, statuses: Sequence[NewsStatus]) -> QueuedNews | None:
        """Захватывает новость по id без ожидания чужой блокировки."""
        raise NotImplementedError

    async def get(self, news_id: int) -> QueuedNews | None:
        """Новость по id."""
        raise NotImplementedError


class PublishPostRepo(Protocol):
    """Нужная стадии часть ProcessedPostRepository."""

    async def get_by_news(self, news_id: int) -> ProcessedPost | None:
        """Пересказ новости."""
        raise NotImplementedError


class PublishSourceRepo(Protocol):
    """Нужная стадии часть SourceRepository."""

    async def get(self, source_id: int) -> PostSource | None:
        """Источник по id."""
        raise NotImplementedError


class PublicationRepo(Protocol):
    """Нужная стадии часть PublicationRepository."""

    async def add(self, publication: Publication) -> Any:
        """Сохраняет запись о публикации."""
        raise NotImplementedError

    async def last_published_at(self, channel_id: int) -> datetime | None:
        """Время последней успешной публикации."""
        raise NotImplementedError


class SettingsGetter(Protocol):
    """Нужная стадии часть SettingsRepository."""

    async def get(self, key: str, default: Any = None) -> Any:
        """Значение настройки."""
        raise NotImplementedError


class PostSender(Protocol):
    """Отправка в канал (Publisher подходит структурно)."""

    async def send(self, fitted: FittedPost, image_url: str | None, link: str | None = None) -> int:
        """Отправляет пост и возвращает message_id."""
        raise NotImplementedError


@dataclass(frozen=True)
class PublishDeps:
    """Зависимости стадии публикации.

    Attributes:
        session_scope: Открывает транзакцию БД.
        news_repo, post_repo, source_repo, publication_repo, settings_repo: Репозитории.
        check_image: Проверка картинки по URL (check_image с HTTP-клиентом).
        publisher: Отправка в канал.
        sink: Получатель алертов.
        cfg: Секция publisher.
        channel_id: Канал для публикации.
        tz: Часовой пояс канала (app.timezone) для тихих часов.
    """

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    news_repo: Callable[[Any], PublishNewsRepo]
    post_repo: Callable[[Any], PublishPostRepo]
    source_repo: Callable[[Any], PublishSourceRepo]
    publication_repo: Callable[[Any], PublicationRepo]
    settings_repo: Callable[[Any], SettingsGetter]
    check_image: Callable[[str | None], Awaitable[ImageCheck]]
    publisher: PostSender
    sink: AlertSink
    cfg: PublisherConfig
    channel_id: int
    tz: str | ZoneInfo


@dataclass(frozen=True)
class PublishReport:
    """Итог вызова: что опубликовано или почему ничего.

    Attributes:
        news_id: Обработанная новость.
        message_id: message_id опубликованного поста.
        skipped: Причина бездействия: paused, slot, empty.
        next_slot: Ближайший разрешённый слот (если ещё не наступил).
        error: Текст ошибки публикации.
    """

    news_id: int | None = None
    message_id: int | None = None
    skipped: str | None = None
    next_slot: datetime | None = None
    error: str | None = None


async def publish_due(now: datetime, deps: PublishDeps) -> PublishReport:
    """Публикует не больше одного поста, если наступил разрешённый слот.

    Пауза -> выход; слот (интервал и тихие часы) в будущем -> выход; иначе
    захватывается пост с наибольшим приоритетом в статусе approved или scheduled
    (оценка интереса с поправкой score_decay_per_h на возраст, SKIP LOCKED),
    проверяется картинка (неподходящая — пост без фото), пост собирается,
    подгоняется под лимит и отправляется. Успех — Publication(sent) и published;
    PublishError — Publication(failed), failed и алерт publish_error.

    Args:
        now: Текущий момент.
        deps: Зависимости.

    Returns:
        Отчёт о вызове.
    """
    async with deps.session_scope() as session:
        if await deps.settings_repo(session).get(PAUSED_SETTING, False):
            return PublishReport(skipped="paused")
        publications = deps.publication_repo(session)
        last = await publications.last_published_at(deps.channel_id)
        slot = next_slot(now, last, deps.cfg.min_interval_min, deps.cfg.quiet_window, deps.tz)
        if slot > now:
            return PublishReport(skipped="slot", next_slot=slot)

        news_repo = deps.news_repo(session)
        item = await news_repo.claim_next(PUBLISHABLE_STATUSES, deps.cfg.score_decay_per_h)
        if item is None:
            return PublishReport(skipped="empty")
        with bind_context(news_id=item.id):
            return await publish_item(item, now, session, deps)


async def publish_item(
    item: QueuedNews, now: datetime, session: Any, deps: PublishDeps
) -> PublishReport:
    """Публикует захваченную новость в открытой транзакции.

    Проверяет картинку, собирает пост, подгоняет под лимиты и отправляет.
    Успех — Publication(sent) и published; PublishError — Publication(failed),
    failed и алерт publish_error; нет пересказа или источника — failed.

    Args:
        item: Захваченная новость в статусе approved или scheduled.
        now: Текущий момент.
        session: Сессия БД вызывающего.
        deps: Зависимости стадии публикации.

    Returns:
        Отчёт о публикации.
    """
    news_repo = deps.news_repo(session)
    post = await deps.post_repo(session).get_by_news(item.id)
    source = await deps.source_repo(session).get(item.source_id)
    if post is None or source is None:
        log.error("publish.missing_data", has_post=post is not None, has_source=source is not None)
        await news_repo.set_status(item.id, item.status, NewsStatus.FAILED, REASON_NO_POST)
        return PublishReport(news_id=item.id, error=REASON_NO_POST)

    image = await deps.check_image(item.image_url)
    if not image.ok:
        log.info("publish.image_skipped", reason=image.reason)
    fitted = fit_post(build_post(post, item, source), has_photo=image.ok, cfg=deps.cfg)
    publications = deps.publication_repo(session)
    try:
        message_id = await deps.publisher.send(fitted, item.image_url)
    except PublishError as exc:
        await publications.add(
            Publication(
                processed_post_id=post.id,
                channel_id=deps.channel_id,
                status=PublicationStatus.FAILED,
                error=str(exc),
            )
        )
        await news_repo.set_status(item.id, item.status, NewsStatus.FAILED, REASON_PUBLISH_ERROR)
        log.error("publish.failed", error=str(exc))
        await deps.sink.emit(
            AlertEvent(
                kind=AlertKind.PUBLISH_ERROR,
                key=f"{AlertKind.PUBLISH_ERROR.value}:{item.id}",
                message=f"Не удалось опубликовать «{post.ai_title}»: {exc}",
                created_at=now,
            )
        )
        return PublishReport(news_id=item.id, error=str(exc))

    await publications.add(
        Publication(
            processed_post_id=post.id,
            channel_id=deps.channel_id,
            status=PublicationStatus.SENT,
            message_id=message_id,
            published_at=now,
        )
    )
    await news_repo.set_status(item.id, item.status, NewsStatus.PUBLISHED)
    log.info("publish.sent", message_id=message_id, with_photo=fitted.use_photo)
    return PublishReport(news_id=item.id, message_id=message_id)


class ManualOutcome(StrEnum):
    """Итог ручной публикации."""

    PUBLISHED = "published"
    NOT_FOUND = "not_found"
    BUSY = "busy"
    ALREADY_DONE = "already_done"
    FAILED = "failed"


@dataclass(frozen=True)
class ManualPublishDeps:
    """Зависимости ручной публикации.

    Attributes:
        publish: Зависимости обычной стадии публикации.
        news_repo: Репозиторий новостей с захватом по id.
    """

    publish: PublishDeps
    news_repo: Callable[[Any], ManualNewsRepo]


@dataclass(frozen=True)
class ManualPublishResult:
    """Результат ручной публикации.

    Attributes:
        outcome: Итог.
        news_id: Новость.
        message_id: message_id поста в канале (для published).
        status: Текущий статус новости (для already_done).
        error: Текст ошибки (для failed).
    """

    outcome: ManualOutcome
    news_id: int
    message_id: int | None = None
    status: NewsStatus | None = None
    error: str | None = None


async def publish_now(
    news_id: int, now: datetime, deps: ManualPublishDeps, initiator_id: int
) -> ManualPublishResult:
    """Публикует пост сразу, вне очереди, по решению администратора.

    Не учитывает приоритет очереди, интервал, тихие часы и паузу; учитывает лимиты,
    проверку картинки, машину состояний и блокировку строки. Пост из pending_review
    сначала переводится в approved. Всё — в одной транзакции.

    Args:
        news_id: Новость.
        now: Текущий момент.
        deps: Зависимости.
        initiator_id: Telegram ID администратора (для лога).

    Returns:
        Результат: published, not_found, busy, already_done или failed.
    """
    async with deps.publish.session_scope() as session:
        result = await _publish_now(news_id, now, session, deps)
    log.info(
        "publish.manual", news_id=news_id, initiator_id=initiator_id, outcome=result.outcome.value
    )
    return result


async def _publish_now(
    news_id: int, now: datetime, session: Any, deps: ManualPublishDeps
) -> ManualPublishResult:
    news_repo = deps.news_repo(session)
    item = await news_repo.claim_one(news_id, MANUAL_STATUSES)
    if item is None:
        current = await news_repo.get(news_id)
        if current is None:
            return ManualPublishResult(ManualOutcome.NOT_FOUND, news_id)
        if current.status in MANUAL_STATUSES:
            return ManualPublishResult(ManualOutcome.BUSY, news_id, status=current.status)
        return ManualPublishResult(ManualOutcome.ALREADY_DONE, news_id, status=current.status)

    if item.status is NewsStatus.PENDING_REVIEW:
        await news_repo.set_status(item.id, NewsStatus.PENDING_REVIEW, NewsStatus.APPROVED)
    with bind_context(news_id=item.id):
        report = await publish_item(item, now, session, deps.publish)
    if report.error is not None:
        return ManualPublishResult(
            ManualOutcome.FAILED, news_id, status=NewsStatus.FAILED, error=report.error
        )
    return ManualPublishResult(ManualOutcome.PUBLISHED, news_id, message_id=report.message_id)
