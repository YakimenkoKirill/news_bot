import itertools
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import source_add
from app.bot.handlers.source_add import SourceAddDeps, normalize_address
from app.bot.keyboards import (
    ADD_FIELD_CONFIRM,
    ADD_FIELD_MODERATION,
    ADD_FIELD_TYPE,
    CONFIRM_CHECK,
    CONFIRM_SAVE,
    AddSourceChoice,
)
from app.collectors.base import BaseCollector, RawItem, TemporaryCollectorError
from app.config import Settings
from app.db.models import Source
from app.domain.enums import ModerationMode, SourceType
from tests.bot_helpers import callback_update, make_bot, message_update
from tests.config_helpers import bot_config

ADMIN = 111
MIN_INTERVAL = 5


@dataclass
class Db:
    sources: list[Source] = field(default_factory=list)
    reloads: int = 0


class Repo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def list_all(self) -> Sequence[Source]:
        return list(self.db.sources)

    async def add(self, source: Source) -> Source:
        source.id = len(self.db.sources) + 1
        self.db.sources.append(source)
        return source


class FakeCollector(BaseCollector):
    def __init__(self, result: list[RawItem] | Exception) -> None:
        self.result = result
        self.seen: list[Source] = []

    async def fetch(self, source: Source) -> list[RawItem]:
        self.seen.append(source)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Dialog:
    """Один диспетчер (с памятью FSM) на весь диалог."""

    def __init__(self, db: Db, collector: BaseCollector | None = None) -> None:
        @asynccontextmanager
        async def scope() -> AsyncIterator[Db]:
            yield db

        async def reload() -> None:
            db.reloads += 1

        class NoAdmins:
            async def is_admin(self, telegram_id: int) -> bool:
                return False

        collectors = {SourceType.RSS: collector, SourceType.WEB: collector} if collector else {}
        deps = BotDeps(
            settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
            admin_repo=NoAdmins(),
            routers=[source_add.create_router()],
            workflow_data={
                "source_add_deps": SourceAddDeps(
                    session_scope=scope,
                    source_repo=Repo,
                    collectors=collectors,
                    reload_telegram=reload,
                    min_interval_min=MIN_INTERVAL,
                )
            },
        )
        self.dispatcher = create_dispatcher(deps)
        self.bot, self.session = make_bot()
        self.ids = itertools.count(1)

    async def say(self, text: str) -> list[str]:
        before = len(self.session.sent_texts())
        await self.dispatcher.feed_update(self.bot, message_update(ADMIN, text, next(self.ids)))
        return self.session.sent_texts()[before:]

    async def press(self, field_name: str, value: str) -> list[str]:
        before = len(self.session.sent_texts())
        data = AddSourceChoice(field=field_name, value=value).pack()
        await self.dispatcher.feed_update(self.bot, callback_update(ADMIN, data, next(self.ids)))
        return self.session.sent_texts()[before:]


def _item(title: str) -> RawItem:
    url = f"https://e.com/{title}"
    return RawItem(source_id=1, external_id=title, url=url, title=title, text="t")


async def _fill(dialog: Dialog, type_: str, url: str, moderation: str = "auto") -> None:
    await dialog.say("/add_source")
    await dialog.press(ADD_FIELD_TYPE, type_)
    await dialog.say(url)
    await dialog.say("Мой источник")
    await dialog.say("15")
    await dialog.press(ADD_FIELD_MODERATION, moderation)


async def test_full_flow_rss() -> None:
    db = Db()
    dialog = Dialog(db)
    assert await dialog.say("/add_source") == [texts.ADD_ASK_TYPE]
    assert await dialog.press(ADD_FIELD_TYPE, "rss") == [texts.ADD_ASK_URL["rss"]]
    assert await dialog.say("https://news.example/rss") == [texts.ADD_ASK_NAME]
    ask_interval = texts.ADD_ASK_INTERVAL.format(min_value=MIN_INTERVAL)
    assert await dialog.say("  Новости  ") == [ask_interval]
    assert await dialog.say("15") == [texts.ADD_ASK_MODERATION]
    [summary] = await dialog.press(ADD_FIELD_MODERATION, "review")
    assert "Адрес: https://news.example/rss" in summary
    assert "Модерация: review" in summary
    assert await dialog.press(ADD_FIELD_CONFIRM, CONFIRM_SAVE) == [
        texts.ADD_SAVED.format(source_id=1, name="Новости")
    ]
    [source] = db.sources
    assert (source.type, source.url, source.name, source.interval_min) == (
        SourceType.RSS,
        "https://news.example/rss",
        "Новости",
        15,
    )
    assert source.moderation_mode is ModerationMode.REVIEW
    assert db.reloads == 0


async def test_full_flow_web_with_selectors() -> None:
    db = Db()
    dialog = Dialog(db)
    await _fill(dialog, "web", "https://site.example/news/")
    replies = await dialog.say('{"list_item": "article", "link": "a.more"}')
    assert "Селекторы:" in replies[0]
    await dialog.press(ADD_FIELD_CONFIRM, CONFIRM_SAVE)
    [source] = db.sources
    assert source.type is SourceType.WEB
    assert source.selectors == {"list_item": "article", "link": "a.more"}


async def test_full_flow_tg_normalizes_username_and_reloads() -> None:
    db = Db()
    dialog = Dialog(db)
    await _fill(dialog, "tg", "t.me/tech_news_ru")
    await dialog.press(ADD_FIELD_CONFIRM, CONFIRM_SAVE)
    [source] = db.sources
    assert source.url == "https://t.me/tech_news_ru"
    assert db.reloads == 1


async def test_input_errors_repeat_question() -> None:
    dialog = Dialog(Db())
    await dialog.say("/add_source")
    await dialog.press(ADD_FIELD_TYPE, "rss")
    assert await dialog.say("ftp://bad.example") == [texts.ADD_BAD_URL]
    await dialog.say("https://ok.example/rss")
    assert await dialog.say("   ") == [texts.ADD_BAD_NAME.format(max_len=255)]
    await dialog.say("Имя")
    bad_interval = texts.ADD_BAD_INTERVAL.format(min_value=MIN_INTERVAL)
    assert await dialog.say("3") == [bad_interval]
    assert await dialog.say("пять") == [bad_interval]
    assert await dialog.say("5") == [texts.ADD_ASK_MODERATION]


async def test_bad_selectors_repeat_with_reason() -> None:
    dialog = Dialog(Db())
    await _fill(dialog, "web", "https://site.example/")
    [reply] = await dialog.say("не json")
    assert reply.startswith("Селекторы не подошли: не JSON")
    [reply] = await dialog.say('{"list_item": "div[["}')
    assert reply.startswith("Селекторы не подошли:")
    assert "CSS" in reply


async def test_bad_tg_username() -> None:
    dialog = Dialog(Db())
    await dialog.say("/add_source")
    await dialog.press(ADD_FIELD_TYPE, "tg")
    assert await dialog.say("https://example.com/channel") == [texts.ADD_BAD_TG]


async def test_cancel_on_any_step() -> None:
    db = Db()
    dialog = Dialog(db)
    await dialog.say("/add_source")
    await dialog.press(ADD_FIELD_TYPE, "rss")
    await dialog.say("https://news.example/rss")
    assert await dialog.say("/cancel") == [texts.ADD_CANCELLED]
    assert await dialog.say("Имя после отмены") == []
    assert await dialog.say("/cancel") == [texts.NOTHING_TO_CANCEL]
    assert db.sources == []


async def test_duplicate_url_exits() -> None:
    existing = Source(
        id=1, type=SourceType.RSS, url="https://news.example/rss", name="x", interval_min=10
    )
    db = Db(sources=[existing])
    dialog = Dialog(db)
    await dialog.say("/add_source")
    await dialog.press(ADD_FIELD_TYPE, "rss")
    assert await dialog.say("https://news.example/rss") == [texts.ADD_DUPLICATE]
    assert await dialog.say("Имя") == []


async def test_trial_collect_success_then_save() -> None:
    db = Db()
    collector = FakeCollector([_item("A"), _item("B"), _item("C"), _item("D")])
    dialog = Dialog(db, collector)
    await _fill(dialog, "rss", "https://news.example/rss")
    assert await dialog.press(ADD_FIELD_CONFIRM, CONFIRM_CHECK) == [
        "Пробный сбор: найдено материалов — 4\n• A\n• B\n• C"
    ]
    assert db.sources == []
    await dialog.press(ADD_FIELD_CONFIRM, CONFIRM_SAVE)
    assert len(db.sources) == 1


async def test_trial_collect_error_does_not_block_save() -> None:
    db = Db()
    dialog = Dialog(db, FakeCollector(TemporaryCollectorError("timeout")))
    await _fill(dialog, "rss", "https://news.example/rss")
    replies = await dialog.press(ADD_FIELD_CONFIRM, CONFIRM_CHECK)
    assert replies == ["Пробный сбор: ошибка — timeout"]
    await dialog.press(ADD_FIELD_CONFIRM, CONFIRM_SAVE)
    assert len(db.sources) == 1


@pytest.mark.parametrize(
    ("type_", "raw", "expected"),
    [
        (SourceType.TG, "@tech_news", "https://t.me/tech_news"),
        (SourceType.TG, "https://t.me/tech_news/", "https://t.me/tech_news"),
        (SourceType.TG, "@bad", None),
        (SourceType.RSS, " https://e.com/rss ", "https://e.com/rss"),
        (SourceType.WEB, "e.com/news", None),
    ],
)
def test_normalize_address(type_: SourceType, raw: str, expected: str | None) -> None:
    assert normalize_address(type_, raw) == expected


def test_real_repository_fits_protocol() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.sources import SourceRepository

    deps = SourceAddDeps(
        session_scope=MagicMock(),
        source_repo=SourceRepository,
        collectors={},
        reload_telegram=None,
        min_interval_min=MIN_INTERVAL,
    )
    assert deps.min_interval_min == MIN_INTERVAL
