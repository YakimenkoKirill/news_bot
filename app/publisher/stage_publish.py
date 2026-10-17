"""Стадия публикации: один пост в разрешённый слот.

Публикация идёт в три шага, чтобы транзакция БД не держалась во время сетевых
вызовов: короткая транзакция захвата (approved -> scheduled и снятие данных
поста с ORM), затем проверка картинки и отправка в Telegram без транзакции, и
короткая транзакция записи итога (scheduled -> published или failed).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog

from app.config import PublisherConfig
from app.db.models import ProcessedPost, Publication
from app.db.repositories.news import Priority, StaleStatusError, StuckRef
from app.domain.alerts import AlertEvent, AlertKind, AlertSink
from app.domain.enums import NewsStatus, PublicationStatus
from app.logging import bind_context
from app.publisher.formatter import PostNews, PostSource, build_post
from app.publisher.limits import FittedPost, fit_post
from app.publisher.media import ImageCheck, PhotoFetcher
from app.publisher.publisher import PublishError
from app.publisher.slots import next_slot
from app.runtime_config import RuntimeSource

log = structlog.get_logger(__name__)

PAUSED_SETTING = "paused"
PUBLISHABLE_STATUSES = (NewsStatus.APPROVED,)
MANUAL_STATUSES = (NewsStatus.APPROVED, NewsStatus.PENDING_REVIEW)
REASON_PUBLISH_ERROR = "publish_error"
REASON_NO_POST = "no_processed_post"
REASON_PUBLISH_UNKNOWN = "publish_unknown"
STUCK_ALERT_HINT = (
    "Проверьте канал: пост мог выйти; если не вышел — /errors -> Новости -> Повторить"
)


class QueuedNews(PostNews, Protocol):
    """Новость в очереди публикации (NewsItem подходит структурно)."""

    id: int
    source_id: int
    status: NewsStatus

    @property
    def raw_meta(self) -> Mapping[str, Any]:
        """Служебные данные сборщика (msg_id и has_photo для фото Telegram)."""
        raise NotImplementedError


class PublishNewsRepo(Protocol):
    """Нужная стадии часть NewsRepository."""

    async def claim_next(
        self, statuses: Sequence[NewsStatus], priority: Priority
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

    async def list_stuck_scheduled(self, before: datetime) -> Sequence[StuckRef]:
        """Новости, застрявшие в статусе scheduled."""
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

    async def send(
        self, fitted: FittedPost, photo: str | bytes | None, link: str | None = None
    ) -> int:
        """Отправляет пост и возвращает message_id."""
        raise NotImplementedError


@dataclass
class PostSnapshot:
    """Пересказ, снятый с ORM (структурно подходит PostContent).

    Attributes:
        ai_title: Заголовок.
        ai_summary: Пересказ.
        hashtags: Хэштеги.
    """

    ai_title: str
    ai_summary: str
    hashtags: list[str]


@dataclass
class NewsSnapshot:
    """Новость, снятая с ORM (структурно подходит PostNews и PhotoNews).

    Attributes:
        url: Адрес источника новости.
        image_url: Картинка новости.
        source_id: Источник новости.
        raw_meta: Служебные данные сборщика (для фото из Telegram).
    """

    url: str
    image_url: str | None
    source_id: int = 0
    raw_meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class SourceSnapshot:
    """Источник, снятый с ORM (структурно подходит PostSource).

    Attributes:
        name: Имя источника.
    """

    name: str


@dataclass(frozen=True)
class ClaimedPost:
    """Пост, захваченный к отправке: всё нужное снято с ORM до закрытия транзакции.

    Attributes:
        news_id: Новость в статусе scheduled.
        post_id: Пересказ (processed_posts.id) для записи Publication.
        post: Пересказ.
        news: Новость.
        source: Источник.
    """

    news_id: int
    post_id: int
    post: PostSnapshot
    news: NewsSnapshot
    source: SourceSnapshot


@dataclass(frozen=True)
class PublishDeps:
    """Зависимости стадии публикации.

    Attributes:
        session_scope: Открывает транзакцию БД.
        news_repo, post_repo, source_repo, publication_repo, settings_repo: Репозитории.
        check_image: Проверка картинки по URL (check_image с HTTP-клиентом).
        photo_fetcher: Загрузка фото из Telegram-источника; None — только по URL.
        runtime: Изменяемые на лету параметры публикации; None — только cfg из yaml.
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
    photo_fetcher: PhotoFetcher | None = None
    runtime: RuntimeSource | None = None


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
    захватывается пост с наибольшим приоритетом в статусе approved (оценка
    интереса с поправкой score_decay_per_h на возраст, SKIP LOCKED), переводится
    в scheduled, и уже вне транзакции проверяется картинка (неподходящая — пост
    без фото), собирается пост и отправляется. Успех — Publication(sent) и
    published; PublishError — Publication(failed), failed и алерт publish_error.

    Args:
        now: Текущий момент.
        deps: Зависимости.

    Returns:
        Отчёт о вызове.
    """
    cfg = await publisher_config(deps)
    async with deps.session_scope() as session:
        if await deps.settings_repo(session).get(PAUSED_SETTING, False):
            return PublishReport(skipped="paused")
        publications = deps.publication_repo(session)
        last = await publications.last_published_at(deps.channel_id)
        slot = next_slot(now, last, cfg.min_interval_min, cfg.quiet_window, deps.tz)
        if slot > now:
            return PublishReport(skipped="slot", next_slot=slot)

        item = await deps.news_repo(session).claim_next(
            PUBLISHABLE_STATUSES, Priority.from_config(cfg)
        )
        if item is None:
            return PublishReport(skipped="empty")
        news_id = item.id
        with bind_context(news_id=news_id):
            claimed = await _claim_for_send(item, item.status, session, deps)

    if claimed is None:
        return PublishReport(news_id=news_id, error=REASON_NO_POST)
    with bind_context(news_id=news_id):
        return await _send_claimed(claimed, now, deps)


async def publisher_config(deps: PublishDeps) -> PublisherConfig:
    """Параметры публикации: снимок изменяемых значений или cfg из yaml.

    Args:
        deps: Зависимости стадии публикации.

    Returns:
        Секция publisher, по которой считать слот, приоритет и лимиты.
    """
    if deps.runtime is None:
        return deps.cfg
    return (await deps.runtime.snapshot()).publisher


async def _claim_for_send(
    item: QueuedNews, expected: NewsStatus, session: Any, deps: PublishDeps
) -> ClaimedPost | None:
    """Переводит новость в scheduled и снимает данные поста с ORM.

    Args:
        item: Захваченная новость.
        expected: Её текущий статус (approved).
        session: Открытая транзакция вызывающего.
        deps: Зависимости стадии публикации.

    Returns:
        Данные для отправки или None, если нет пересказа или источника; в этом
        случае новость переведена в failed с причиной no_processed_post.
    """
    news_repo = deps.news_repo(session)
    await news_repo.set_status(item.id, expected, NewsStatus.SCHEDULED)
    post = await deps.post_repo(session).get_by_news(item.id)
    source = await deps.source_repo(session).get(item.source_id)
    if post is None or source is None:
        log.error("publish.missing_data", has_post=post is not None, has_source=source is not None)
        await news_repo.set_status(item.id, NewsStatus.SCHEDULED, NewsStatus.FAILED, REASON_NO_POST)
        return None
    return ClaimedPost(
        news_id=item.id,
        post_id=post.id,
        post=PostSnapshot(
            ai_title=post.ai_title, ai_summary=post.ai_summary, hashtags=list(post.hashtags)
        ),
        news=NewsSnapshot(
            url=item.url,
            image_url=item.image_url,
            source_id=item.source_id,
            raw_meta=dict(item.raw_meta or {}),
        ),
        source=SourceSnapshot(name=source.name),
    )


async def _send_claimed(
    claimed: ClaimedPost,
    now: datetime,
    deps: PublishDeps,
    *,
    initiated_by: int | None = None,
) -> PublishReport:
    """Отправляет захваченный пост вне транзакции и записывает итог.

    Args:
        claimed: Данные поста в статусе scheduled.
        now: Текущий момент.
        deps: Зависимости стадии публикации.
        initiated_by: Telegram ID администратора при публикации вне очереди
            (None — обычная публикация по расписанию).

    Returns:
        Отчёт о публикации.
    """
    photo = await _photo(claimed, deps)
    post = build_post(claimed.post, claimed.news, claimed.source, deps.cfg)
    fitted = fit_post(post, has_photo=photo is not None, cfg=deps.cfg)
    try:
        message_id = await deps.publisher.send(fitted, photo)
    except PublishError as exc:
        await _record_failure(claimed, exc, now, deps, initiated_by)
        return PublishReport(news_id=claimed.news_id, error=str(exc))

    async with deps.session_scope() as session:
        await deps.publication_repo(session).add(
            Publication(
                processed_post_id=claimed.post_id,
                channel_id=deps.channel_id,
                status=PublicationStatus.SENT,
                message_id=message_id,
                published_at=now,
                initiated_by=initiated_by,
            )
        )
        await deps.news_repo(session).set_status(
            claimed.news_id, NewsStatus.SCHEDULED, NewsStatus.PUBLISHED
        )
    log.info("publish.sent", message_id=message_id, with_photo=fitted.use_photo)
    return PublishReport(news_id=claimed.news_id, message_id=message_id)


async def _photo(claimed: ClaimedPost, deps: PublishDeps) -> str | bytes | None:
    """Картинка поста: проверенный URL или фото из Telegram-источника.

    У новостей из Telegram публичного адреса картинки нет, поэтому фото
    скачивается из исходного сообщения. Любая ошибка загрузки оставляет пост
    без фото: текст важнее картинки.

    Args:
        claimed: Данные захваченного поста.
        deps: Зависимости стадии публикации.

    Returns:
        Адрес картинки, байты фото или None.
    """
    url = claimed.news.image_url
    if url:
        image = await deps.check_image(url)
        if image.ok:
            return url
        log.info("publish.image_skipped", reason=image.reason)
        return None
    if deps.photo_fetcher is None:
        return None
    try:
        return await deps.photo_fetcher.fetch(claimed.news)
    except Exception as exc:
        log.warning("publish.tg_photo_failed", error=repr(exc))
        return None


async def _record_failure(
    claimed: ClaimedPost,
    exc: PublishError,
    now: datetime,
    deps: PublishDeps,
    initiated_by: int | None,
) -> None:
    async with deps.session_scope() as session:
        await deps.publication_repo(session).add(
            Publication(
                processed_post_id=claimed.post_id,
                channel_id=deps.channel_id,
                status=PublicationStatus.FAILED,
                error=str(exc),
                initiated_by=initiated_by,
            )
        )
        await deps.news_repo(session).set_status(
            claimed.news_id, NewsStatus.SCHEDULED, NewsStatus.FAILED, REASON_PUBLISH_ERROR
        )
    log.error("publish.failed", error=str(exc))
    await deps.sink.emit(
        AlertEvent(
            kind=AlertKind.PUBLISH_ERROR,
            key=f"{AlertKind.PUBLISH_ERROR.value}:{claimed.news_id}",
            message=f"Не удалось опубликовать «{claimed.post.ai_title}»: {exc}",
            created_at=now,
        )
    )


async def recover_stuck(now: datetime, deps: PublishDeps) -> int:
    """Разбирает посты, застрявшие в отправке дольше publisher.stuck_after_min.

    Пост остаётся в scheduled, если приложение упало между отправкой в Telegram и
    записью итога. Есть Publication(sent) — пост дошёл до канала, и новость
    переводится в published. Записи нет — неизвестно, вышел ли пост: новость
    переводится в failed с причиной publish_unknown, уходит алерт publish_error,
    повторная отправка остаётся за администратором.

    Args:
        now: Текущий момент.
        deps: Зависимости стадии публикации.

    Returns:
        Сколько новостей разобрано.
    """
    cfg = await publisher_config(deps)
    border = now - timedelta(minutes=cfg.stuck_after_min)
    async with deps.session_scope() as session:
        stuck = list(await deps.news_repo(session).list_stuck_scheduled(border))
    recovered = 0
    for ref in stuck:
        with bind_context(news_id=ref.news_id):
            if await _recover_one(ref, now, deps):
                recovered += 1
    if recovered:
        log.warning("publish.stuck_recovered", count=recovered)
    return recovered


async def _recover_one(ref: StuckRef, now: datetime, deps: PublishDeps) -> bool:
    new_status = NewsStatus.PUBLISHED if ref.sent else NewsStatus.FAILED
    reason = None if ref.sent else REASON_PUBLISH_UNKNOWN
    try:
        async with deps.session_scope() as session:
            await deps.news_repo(session).set_status(
                ref.news_id, NewsStatus.SCHEDULED, new_status, reason
            )
    except StaleStatusError:
        log.info("publish.stuck_skipped", news_id=ref.news_id)
        return False
    log.warning("publish.stuck", news_id=ref.news_id, sent=ref.sent, status=new_status.value)
    if ref.sent:
        return True
    await deps.sink.emit(
        AlertEvent(
            kind=AlertKind.PUBLISH_ERROR,
            key=f"{AlertKind.PUBLISH_ERROR.value}:{ref.news_id}",
            message=f"Пост «{ref.title}» застрял в отправке. {STUCK_ALERT_HINT}",
            created_at=now,
        )
    )
    return True


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

    Не учитывает приоритет очереди, интервал, тихие часы и паузу; учитывает лимиты
    длины, проверку картинки, машину состояний и блокировку строки. Лимит постов
    источника в очереди тоже не проверяется: пост сразу уходит в канал и в очереди
    не остаётся. Пост из pending_review сначала переводится в approved, затем, как
    и обычная публикация, в scheduled; пост, который уже отправляется (scheduled),
    даёт busy. Отправка идёт вне транзакции.

    Args:
        news_id: Новость.
        now: Текущий момент.
        deps: Зависимости.
        initiator_id: Telegram ID администратора (для лога).

    Returns:
        Результат: published, not_found, busy, already_done или failed.
    """
    async with deps.publish.session_scope() as session:
        with bind_context(news_id=news_id):
            claim = await _claim_manual(news_id, session, deps)
    if isinstance(claim, ManualPublishResult):
        result = claim
    else:
        with bind_context(news_id=news_id):
            report = await _send_claimed(claim, now, deps.publish, initiated_by=initiator_id)
        result = _manual_result(news_id, report)
    log.info(
        "publish.manual", news_id=news_id, initiator_id=initiator_id, outcome=result.outcome.value
    )
    return result


async def _claim_manual(
    news_id: int, session: Any, deps: ManualPublishDeps
) -> ClaimedPost | ManualPublishResult:
    news_repo = deps.news_repo(session)
    item = await news_repo.claim_one(news_id, MANUAL_STATUSES)
    if item is None:
        current = await news_repo.get(news_id)
        if current is None:
            return ManualPublishResult(ManualOutcome.NOT_FOUND, news_id)
        if current.status in (*MANUAL_STATUSES, NewsStatus.SCHEDULED):
            return ManualPublishResult(ManualOutcome.BUSY, news_id, status=current.status)
        return ManualPublishResult(ManualOutcome.ALREADY_DONE, news_id, status=current.status)

    if item.status is NewsStatus.PENDING_REVIEW:
        await news_repo.set_status(item.id, NewsStatus.PENDING_REVIEW, NewsStatus.APPROVED)
    claimed = await _claim_for_send(item, NewsStatus.APPROVED, session, deps.publish)
    if claimed is None:
        return ManualPublishResult(
            ManualOutcome.FAILED, news_id, status=NewsStatus.FAILED, error=REASON_NO_POST
        )
    return claimed


def _manual_result(news_id: int, report: PublishReport) -> ManualPublishResult:
    if report.error is not None:
        return ManualPublishResult(
            ManualOutcome.FAILED, news_id, status=NewsStatus.FAILED, error=report.error
        )
    return ManualPublishResult(ManualOutcome.PUBLISHED, news_id, message_id=report.message_id)
