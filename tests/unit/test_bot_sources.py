from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aiogram.methods import AnswerCallbackQuery, EditMessageText

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import sources as sources_handlers
from app.bot.handlers.sources import SourcesDeps
from app.bot.keyboards import SourceAction, SourceActionType
from app.collectors.base import BaseCollector, RawItem, TemporaryCollectorError
from app.config import Settings
from app.db.models import Source
from app.domain.enums import SourceType
from tests.bot_helpers import FakeSession, callback_update, make_bot, message_update
from tests.config_helpers import bot_config

ADMIN = 111
PAGE_SIZE = 5


@dataclass
class Db:
    sources: dict[int, Source] = field(default_factory=dict)
    reloads: int = 0


class Repo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def list_all(self) -> Sequence[Source]:
        return [self.db.sources[k] for k in sorted(self.db.sources)]

    async def get(self, source_id: int) -> Source | None:
        return self.db.sources.get(source_id)

    async def set_enabled(self, source_id: int, enabled: bool) -> bool:
        self.db.sources[source_id].enabled = enabled
        return True

    async def delete(self, source_id: int) -> bool:
        return self.db.sources.pop(source_id, None) is not None


class FakeCollector(BaseCollector):
    def __init__(self, result: list[RawItem] | Exception) -> None:
        self.result = result

    async def fetch(self, source: Source) -> list[RawItem]:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _source(source_id: int, type: SourceType = SourceType.RSS, enabled: bool = True) -> Source:
    return Source(
        id=source_id,
        type=type,
        url=f"https://s{source_id}.example/",
        name=f"Источник {source_id}",
        interval_min=10,
        enabled=enabled,
        error_count=0,
    )


def _db(count: int, **kwargs: Any) -> Db:
    return Db(sources={i: _source(i, **kwargs) for i in range(1, count + 1)})


def _deps(db: Db, collector: BaseCollector | None = None) -> SourcesDeps:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Db]:
        yield db

    async def reload() -> None:
        db.reloads += 1

    return SourcesDeps(
        session_scope=scope,
        source_repo=Repo,
        collectors={SourceType.RSS: collector} if collector else {},
        reload_telegram=reload,
        page_size=PAGE_SIZE,
    )


async def _feed(db: Db, update: Any, collector: BaseCollector | None = None) -> FakeSession:
    class NoAdmins:
        async def is_admin(self, telegram_id: int) -> bool:
            return False

    deps = BotDeps(
        settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
        admin_repo=NoAdmins(),
        routers=[sources_handlers.create_router()],
        workflow_data={"sources_deps": _deps(db, collector)},
    )
    bot, session = make_bot()
    await create_dispatcher(deps).feed_update(bot, update)
    return session


def _cb(action: SourceActionType, source_id: int = 0, page: int = 0) -> Any:
    data = SourceAction(action=action, id=source_id, page=page).pack()
    return callback_update(ADMIN, data)


def _edits(session: FakeSession) -> list[EditMessageText]:
    return [r for r in session.requests if isinstance(r, EditMessageText)]


def _answers(session: FakeSession) -> list[AnswerCallbackQuery]:
    return [r for r in session.requests if isinstance(r, AnswerCallbackQuery)]


async def test_list_with_pagination() -> None:
    db = _db(12)
    session = await _feed(db, message_update(ADMIN, "/sources"))
    [sent] = session.sent()
    assert "страница 1 из 3" in sent.text
    assert "#1 [rss] Источник 1 — вкл" in sent.text
    assert "#6" not in sent.text
    nav = [b.text for b in sent.reply_markup.inline_keyboard[-1]]  # type: ignore[union-attr]
    assert nav == ["▶️"]

    session = await _feed(db, _cb(SourceActionType.PAGE, page=1))
    [edit] = _edits(session)
    edit_text = edit.text or ""
    assert "страница 2 из 3" in edit_text
    assert "#6 [rss]" in edit_text
    nav = [b.text for b in edit.reply_markup.inline_keyboard[-1]]  # type: ignore[union-attr]
    assert nav == ["◀️", "▶️"]


async def test_empty_list_hint() -> None:
    session = await _feed(Db(), message_update(ADMIN, "/sources"))
    assert session.sent_texts() == [texts.NO_SOURCES]


async def test_toggle() -> None:
    db = _db(2)
    await _feed(db, _cb(SourceActionType.TOGGLE, 2))
    assert db.sources[2].enabled is False
    session = await _feed(db, _cb(SourceActionType.TOGGLE, 2))
    assert db.sources[2].enabled is True
    assert "#2 [rss] Источник 2 — вкл" in _edits(session)[0].text
    assert db.reloads == 0


async def test_delete_requires_confirmation() -> None:
    db = _db(2)
    session = await _feed(db, _cb(SourceActionType.DELETE, 1))
    assert 1 in db.sources
    [edit] = _edits(session)
    assert edit.text == texts.confirm_delete("Источник 1")
    buttons = [b.text for b in edit.reply_markup.inline_keyboard[0]]  # type: ignore[union-attr]
    assert buttons == ["Да, удалить", "Отмена"]

    session = await _feed(db, _cb(SourceActionType.CONFIRM_DELETE, 1))
    assert 1 not in db.sources
    assert _answers(session)[-1].text == texts.SOURCE_DELETED


async def test_stale_callback_for_deleted_source() -> None:
    db = _db(1)
    session = await _feed(db, _cb(SourceActionType.TOGGLE, 99))
    [answer] = _answers(session)
    assert answer.text == texts.SOURCE_NOT_FOUND
    assert answer.show_alert is True


async def test_reload_sources_after_telegram_change() -> None:
    db = Db(sources={1: _source(1, SourceType.TG), 2: _source(2)})
    await _feed(db, _cb(SourceActionType.TOGGLE, 1))
    assert db.reloads == 1
    await _feed(db, _cb(SourceActionType.TOGGLE, 2))
    assert db.reloads == 1
    await _feed(db, _cb(SourceActionType.CONFIRM_DELETE, 1))
    assert db.reloads == 2


async def test_check_source() -> None:
    db = _db(1)
    item = RawItem(
        source_id=1, external_id="a", url="https://s1.example/a", title="Новость", text="t"
    )
    session = await _feed(db, _cb(SourceActionType.CHECK, 1), FakeCollector([item, item]))
    assert session.sent_texts() == ["Проверка #1: найдено материалов — 2\nПервый: Новость"]

    failing = FakeCollector(TemporaryCollectorError("timeout"))
    session = await _feed(db, _cb(SourceActionType.CHECK, 1), failing)
    assert session.sent_texts() == ["Проверка #1: ошибка — timeout"]


async def test_check_unsupported_for_telegram() -> None:
    db = Db(sources={1: _source(1, SourceType.TG)})
    session = await _feed(db, _cb(SourceActionType.CHECK, 1))
    assert session.sent_texts() == [texts.CHECK_UNSUPPORTED]


def test_real_repository_fits_protocol() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.sources import SourceRepository

    deps = SourcesDeps(
        session_scope=MagicMock(),
        source_repo=SourceRepository,
        collectors={},
        reload_telegram=None,
        page_size=1,
    )
    assert deps.page_size == 1
