import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl.types import MessageEntityUrl, PeerChannel

from app.collectors.telegram.albums import AlbumBuffer, AlbumMessage, merge_album
from app.collectors.telegram.convert import message_to_raw_item
from app.db.models import Source
from app.domain.enums import SourceType
from tests.config_helpers import telegram_config

WAIT_S = telegram_config().album_wait_s
DATE = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


class ManualSleep:
    """Подменённое время: каждый sleep ждёт, пока тест его не отпустит."""

    def __init__(self) -> None:
        self.waiters: list[tuple[float, asyncio.Future[None]]] = []

    async def __call__(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters.append((seconds, future))
        await future

    def active(self) -> list[asyncio.Future[None]]:
        return [f for _, f in self.waiters if not f.done()]

    async def release_all(self) -> None:
        for future in self.active():
            future.set_result(None)
        await _settle()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class Collected:
    def __init__(self) -> None:
        self.batches: list[list[Any]] = []

    async def __call__(self, messages: list[Any]) -> None:
        self.batches.append(messages)

    def ids(self) -> list[list[int]]:
        return [[m.id for m in batch] for batch in self.batches]


def _msg(
    msg_id: int,
    grouped_id: int | None = None,
    channel_id: int = 100,
    text: str = "",
    photo_id: int | None = None,
    entities: list[Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=msg_id,
        grouped_id=grouped_id,
        message=text,
        entities=entities or [],
        photo=SimpleNamespace(id=photo_id) if photo_id is not None else None,
        date=DATE,
        peer_id=PeerChannel(channel_id=channel_id),
    )


@pytest.fixture
def sleep() -> ManualSleep:
    return ManualSleep()


@pytest.fixture
def collected() -> Collected:
    return Collected()


@pytest.fixture
def buffer(sleep: ManualSleep, collected: Collected) -> AlbumBuffer:
    return AlbumBuffer(WAIT_S, collected, sleep=sleep)


async def test_single_message_passed_immediately(
    buffer: AlbumBuffer, collected: Collected, sleep: ManualSleep
) -> None:
    await buffer.add(_msg(1, text="одиночное"))
    assert collected.ids() == [[1]]
    assert sleep.waiters == []
    assert buffer.pending == 0


async def test_album_of_three_parts(
    buffer: AlbumBuffer, collected: Collected, sleep: ManualSleep
) -> None:
    for msg_id in (10, 11, 12):
        await buffer.add(_msg(msg_id, grouped_id=7, photo_id=msg_id))
        await _settle()
    assert collected.batches == []
    assert len(sleep.active()) == 1
    assert sleep.waiters[-1][0] == WAIT_S
    await sleep.release_all()
    assert collected.ids() == [[10, 11, 12]]
    assert buffer.pending == 0


async def test_new_part_extends_wait(
    buffer: AlbumBuffer, collected: Collected, sleep: ManualSleep
) -> None:
    await buffer.add(_msg(10, grouped_id=7))
    await _settle()
    first_timer = sleep.waiters[0][1]
    await buffer.add(_msg(11, grouped_id=7))
    await _settle()
    assert first_timer.cancelled()
    assert collected.batches == []
    assert len(sleep.active()) == 1
    await sleep.release_all()
    assert collected.ids() == [[10, 11]]


async def test_two_albums_in_parallel_not_mixed(
    buffer: AlbumBuffer, collected: Collected, sleep: ManualSleep
) -> None:
    await buffer.add(_msg(1, grouped_id=7, channel_id=100))
    await buffer.add(_msg(2, grouped_id=7, channel_id=200))
    await buffer.add(_msg(3, grouped_id=8, channel_id=100))
    await buffer.add(_msg(4, grouped_id=7, channel_id=100))
    await _settle()
    assert buffer.pending == 3
    await sleep.release_all()
    assert sorted(collected.ids()) == [[1, 4], [2], [3]]


async def test_flush_all_cancels_timers_and_keeps_messages(
    buffer: AlbumBuffer, collected: Collected, sleep: ManualSleep
) -> None:
    await buffer.add(_msg(1, grouped_id=7))
    await buffer.add(_msg(2, grouped_id=7))
    await buffer.add(_msg(3, grouped_id=9))
    await _settle()
    await buffer.flush_all()
    assert sorted(collected.ids()) == [[1, 2], [3]]
    assert all(future.cancelled() for _, future in sleep.waiters)
    assert buffer.pending == 0
    await buffer.flush_all()
    assert len(collected.batches) == 2


async def test_handler_error_is_logged_not_raised(sleep: ManualSleep) -> None:
    async def failing(messages: list[Any]) -> None:
        raise RuntimeError("handler failed")

    buffer = AlbumBuffer(WAIT_S, failing, sleep=sleep)
    await buffer.add(_msg(1, grouped_id=7))
    await _settle()
    await sleep.release_all()
    assert buffer.pending == 0


def test_merge_album_takes_text_and_all_photos() -> None:
    link = MessageEntityUrl(offset=7, length=len("https://site.example/x"))
    parts = [
        _msg(12, grouped_id=7, photo_id=3),
        _msg(11, grouped_id=7, text="Смотри https://site.example/x", photo_id=2, entities=[link]),
        _msg(10, grouped_id=7, photo_id=1),
    ]
    merged = merge_album(parts)
    assert isinstance(merged, AlbumMessage)
    assert merged.id == 10
    assert merged.message == "Смотри https://site.example/x"
    assert merged.entities == [link]
    assert merged.photo_ids == [1, 2, 3]
    assert merged.grouped_id == 7

    source = Source(id=1, type=SourceType.TG, url="https://t.me/ch", name="ch", interval_min=1)
    item = message_to_raw_item(merged, source, "ch", telegram_config().title_max_chars)
    assert item is not None
    assert item.external_id == "100:10"
    assert item.raw_meta["photo_ids"] == [1, 2, 3]
    assert item.raw_meta["links"] == ["https://site.example/x"]
    assert item.raw_meta["has_photo"] is True


def test_merge_album_without_text() -> None:
    merged = merge_album([_msg(5, grouped_id=1, photo_id=50), _msg(6, grouped_id=1, photo_id=60)])
    assert merged.message == ""
    assert merged.entities == []
    assert merged.photo_ids == [50, 60]


def test_merge_empty_album_rejected() -> None:
    with pytest.raises(ValueError, match="альбом"):
        merge_album([])
