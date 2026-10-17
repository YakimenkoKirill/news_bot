"""Отправка постов в Telegram-канал через aiogram."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, LinkPreviewOptions
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
)

from app.config import PublisherConfig
from app.publisher.limits import FittedPost

log = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]
PHOTO_FILENAME = "photo.jpg"


def as_input_photo(photo: str | bytes) -> str | BufferedInputFile:
    """Готовит картинку для aiogram: байты — файлом, строка — адресом."""
    if isinstance(photo, bytes):
        return BufferedInputFile(photo, filename=PHOTO_FILENAME)
    return photo


def _photo_log(photo: str | bytes) -> str:
    """Короткое описание картинки для логов (байты не логируем целиком)."""
    return f"{len(photo)} байт" if isinstance(photo, bytes) else photo


class PublishError(RuntimeError):
    """Пост не удалось отправить в канал."""


class Publisher:
    """Отправляет подогнанные посты в канал.

    RetryAfter пережидается (не больше max_retries повторов); если Telegram
    отклонил фото, пост один раз отправляется текстом.
    """

    def __init__(
        self,
        bot: Any,
        channel_id: int,
        cfg: PublisherConfig,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._channel_id = channel_id
        self._cfg = cfg
        self._sleep = sleep

    async def send(
        self, fitted: FittedPost, photo: str | bytes | None, link: str | None = None
    ) -> int:
        """Отправляет пост и возвращает message_id.

        Args:
            fitted: Пост, уложенный в лимит (fit_post).
            photo: Картинка (используется при fitted.use_photo): адрес или байты
                фото, скачанного из Telegram-источника.
            link: Ссылка на источник для превью текстового поста.

        Returns:
            message_id опубликованного сообщения.

        Raises:
            PublishError: Telegram отклонил пост или повторы исчерпаны.
        """
        if fitted.use_photo and photo:
            try:
                return await self._with_retry(lambda: self._send_photo(fitted, photo))
            except TelegramBadRequest as exc:
                log.warning("publisher.photo_rejected", error=exc.message, photo=_photo_log(photo))
            except TelegramAPIError as exc:
                raise PublishError(f"Telegram отклонил пост: {exc.message}") from exc
        try:
            return await self._with_retry(lambda: self._send_text(fitted, link))
        except TelegramAPIError as exc:
            raise PublishError(f"Telegram отклонил пост: {exc.message}") from exc

    async def _send_photo(self, fitted: FittedPost, photo: str | bytes) -> int:
        message = await self._bot.send_photo(
            chat_id=self._channel_id,
            photo=as_input_photo(photo),
            caption=fitted.html,
            parse_mode=ParseMode.HTML,
        )
        return int(message.message_id)

    async def _send_text(self, fitted: FittedPost, link: str | None) -> int:
        """Отправляет пост текстом; без явной ссылки превью отключается.

        Иначе Telegram построит карточку по ссылке из подписи канала.
        """
        preview = (
            LinkPreviewOptions(url=link, prefer_large_media=True)
            if link
            else LinkPreviewOptions(is_disabled=True)
        )
        message = await self._bot.send_message(
            chat_id=self._channel_id,
            text=fitted.html,
            parse_mode=ParseMode.HTML,
            link_preview_options=preview,
        )
        return int(message.message_id)

    async def _with_retry(self, call: Callable[[], Awaitable[int]]) -> int:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._cfg.max_retries + 1),
            wait=self._wait,
            retry=retry_if_exception_type(TelegramRetryAfter),
            sleep=self._sleep,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await call()
        except RetryError as exc:
            last = exc.last_attempt.exception()
            raise PublishError(f"Telegram ограничил частоту отправки: {last}") from last
        raise AssertionError("AsyncRetrying завершился без результата")

    @staticmethod
    def _wait(state: RetryCallState) -> float:
        error = state.outcome.exception() if state.outcome else None
        delay = float(error.retry_after) if isinstance(error, TelegramRetryAfter) else 0.0
        log.warning("publisher.retry_after", attempt=state.attempt_number, delay_s=delay)
        return delay
