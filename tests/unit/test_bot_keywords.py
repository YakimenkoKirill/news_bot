import itertools
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from aiogram.methods import AnswerCallbackQuery, EditMessageText

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import keywords as keywords_handlers
from app.bot.handlers.keywords import KeywordsDeps, split_words
from app.bot.keyboards import KW_ADD, KW_DELETE, KW_TYPE, KeywordAction
from app.config import Settings
from app.domain.enums import KeywordType
from tests.bot_helpers import callback_update, make_bot, message_update
from tests.config_helpers import bot_config

ADMIN = 111
MAX_CHARS = 20


@dataclass
class Kw:
    id: int
    word: str
    type: KeywordType


@dataclass
class Db:
    words: dict[int, Kw] = field(default_factory=dict)
    next_id: int = 1


class Repo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def list_by_type(self, type: KeywordType) -> Sequence[Kw]:
        return sorted((k for k in self.db.words.values() if k.type == type), key=lambda k: k.word)

    async def add(self, word: str, type: KeywordType) -> object | None:
        if any(k.word == word and k.type == type for k in self.db.words.values()):
            return None
        keyword = Kw(self.db.next_id, word, type)
        self.db.words[keyword.id] = keyword
        self.db.next_id += 1
        return keyword

    async def delete(self, keyword_id: int) -> bool:
        return self.db.words.pop(keyword_id, None) is not None


class Chat:
    def __init__(self, db: Db) -> None:
        @asynccontextmanager
        async def scope() -> AsyncIterator[Db]:
            yield db

        class NoAdmins:
            async def is_admin(self, telegram_id: int) -> bool:
                return False

        deps = BotDeps(
            settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
            admin_repo=NoAdmins(),
            routers=[keywords_handlers.create_router()],
            workflow_data={
                "keywords_deps": KeywordsDeps(
                    session_scope=scope, keyword_repo=Repo, max_keyword_chars=MAX_CHARS
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

    async def press(self, action: KeywordAction) -> None:
        await self.dispatcher.feed_update(
            self.bot, callback_update(ADMIN, action.pack(), next(self.ids))
        )


def _db(*words: tuple[str, KeywordType]) -> Db:
    db = Db()
    for word, type_ in words:
        db.words[db.next_id] = Kw(db.next_id, word, type_)
        db.next_id += 1
    return db


async def test_show_lists() -> None:
    chat = Chat(_db(("ии", KeywordType.INCLUDE), ("казино", KeywordType.EXCLUDE)))
    [text] = await chat.say("/keywords")
    assert texts.KW_TITLE_INCLUDE in text
    assert "• ии" in text
    assert texts.KW_TITLE_EXCLUDE in text
    assert "• казино" in text
    markup = chat.session.sent()[0].reply_markup
    buttons = [b.text for row in markup.inline_keyboard for b in row]  # type: ignore[union-attr]
    assert buttons == ["❌ ии", "❌ казино", "➕ Добавить"]


async def test_empty_lists() -> None:
    [text] = await Chat(Db()).say("/keywords")
    assert text.count(texts.KW_EMPTY) == 2


async def test_add_with_duplicates_and_long_word() -> None:
    db = _db(("ии", KeywordType.INCLUDE))
    chat = Chat(db)
    await chat.press(KeywordAction(action=KW_ADD))
    await chat.press(KeywordAction(action=KW_TYPE, value="include"))
    long_word = "о" * (MAX_CHARS + 1)
    replies = await chat.say(f"Нейросеть, ИИ, нейросеть,  Ёлка , {long_word}")
    assert replies[0] == (
        "Добавлено: нейросеть, елка\n"
        "Пропущено (уже есть): ии, нейросеть\n"
        f"Отклонено (длиннее {MAX_CHARS} символов): {long_word}"
    )
    assert sorted(k.word for k in db.words.values()) == ["елка", "ии", "нейросеть"]
    assert all(k.type is KeywordType.INCLUDE for k in db.words.values())
    assert "• нейросеть" in replies[1]


async def test_add_stop_words() -> None:
    db = Db()
    chat = Chat(db)
    await chat.press(KeywordAction(action=KW_ADD))
    await chat.press(KeywordAction(action=KW_TYPE, value="exclude"))
    await chat.say("ставки на спорт")
    [keyword] = db.words.values()
    assert (keyword.word, keyword.type) == ("ставки на спорт", KeywordType.EXCLUDE)


async def test_empty_input_repeats_question() -> None:
    db = Db()
    chat = Chat(db)
    await chat.press(KeywordAction(action=KW_ADD))
    await chat.press(KeywordAction(action=KW_TYPE, value="include"))
    assert await chat.say(" , ;  ") == [texts.KW_EMPTY_INPUT]
    replies = await chat.say("робот")
    assert replies[0].startswith("Добавлено: робот")


async def test_delete_and_already_deleted() -> None:
    db = _db(("ии", KeywordType.INCLUDE))
    chat = Chat(db)
    await chat.press(KeywordAction(action=KW_DELETE, id=1))
    assert db.words == {}
    answers = [r for r in chat.session.requests if isinstance(r, AnswerCallbackQuery)]
    assert answers[-1].text == texts.KW_DELETED
    edits = [r for r in chat.session.requests if isinstance(r, EditMessageText)]
    assert edits and "• ии" not in (edits[-1].text or "")

    await chat.press(KeywordAction(action=KW_DELETE, id=1))
    answers = [r for r in chat.session.requests if isinstance(r, AnswerCallbackQuery)]
    assert answers[-1].text == texts.KW_ALREADY_DELETED
    assert answers[-1].show_alert is True


def test_split_words() -> None:
    assert split_words("a, b ;c\nd,,  ") == ["a", "b", "c", "d"]


def test_real_repository_fits_protocol() -> None:
    from unittest.mock import MagicMock

    from app.db.repositories.misc import KeywordRepository

    deps = KeywordsDeps(
        session_scope=MagicMock(), keyword_repo=KeywordRepository, max_keyword_chars=1
    )
    assert deps.max_keyword_chars == 1
