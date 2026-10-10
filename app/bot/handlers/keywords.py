"""Ключевые и стоп-слова: просмотр, добавление (FSM) и удаление."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.keyboards import KW_ADD, KW_DELETE, KW_TYPE, KeywordAction
from app.bot.states import AddKeywords
from app.db.repositories.misc import normalize_keyword
from app.domain.enums import KeywordType

log = structlog.get_logger(__name__)

_SEPARATORS = re.compile(r"[,\n;]")
DELETE_BUTTONS_PER_ROW = 3


class StoredKeyword(Protocol):
    """Слово из БД (Keyword подходит структурно)."""

    id: int
    word: str


class KeywordsRepo(Protocol):
    """Нужная боту часть KeywordRepository."""

    async def list_by_type(self, type: KeywordType) -> Sequence[StoredKeyword]:
        """Слова заданного типа."""
        raise NotImplementedError

    async def add(self, word: str, type: KeywordType) -> object | None:
        """Добавляет слово; None — уже есть."""
        raise NotImplementedError

    async def delete(self, keyword_id: int) -> bool:
        """Удаляет слово; False — его уже нет."""
        raise NotImplementedError


@dataclass(frozen=True)
class KeywordsDeps:
    """Зависимости управления словами (workflow_data: keywords_deps)."""

    session_scope: Callable[[], AbstractAsyncContextManager[Any]]
    keyword_repo: Callable[[Any], KeywordsRepo]
    max_keyword_chars: int


@dataclass(frozen=True)
class AddResult:
    """Итог добавления: добавленные, дубли и слишком длинные слова."""

    added: list[str]
    duplicates: list[str]
    too_long: list[str]


def split_words(text: str) -> list[str]:
    """Слова и фразы из ввода через запятую (пустые отбрасываются)."""
    return [part.strip() for part in _SEPARATORS.split(text) if part.strip()]


async def render_keywords(deps: KeywordsDeps) -> tuple[str, InlineKeyboardMarkup]:
    """Текст с двумя списками и кнопки удаления и добавления."""
    async with deps.session_scope() as session:
        repo = deps.keyword_repo(session)
        include = list(await repo.list_by_type(KeywordType.INCLUDE))
        exclude = list(await repo.list_by_type(KeywordType.EXCLUDE))

    def block(title: str, words: Sequence[StoredKeyword]) -> list[str]:
        items = [f"• {html.escape(k.word)}" for k in words] or [texts.KW_EMPTY]
        return [title, *items]

    lines = [*block(texts.KW_TITLE_INCLUDE, include), "", *block(texts.KW_TITLE_EXCLUDE, exclude)]
    text = "\n".join(lines)
    builder = InlineKeyboardBuilder()
    for keyword in [*include, *exclude]:
        builder.button(
            text=f"❌ {keyword.word}",
            callback_data=KeywordAction(action=KW_DELETE, id=keyword.id),
        )
    builder.adjust(DELETE_BUTTONS_PER_ROW)
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data=KeywordAction(action=KW_ADD).pack())
    )
    return text, builder.as_markup()


async def add_words(deps: KeywordsDeps, words: Sequence[str], type_: KeywordType) -> AddResult:
    """Нормализует и сохраняет слова; дубли и слишком длинные пропускаются."""
    added: list[str] = []
    duplicates: list[str] = []
    too_long: list[str] = []
    async with deps.session_scope() as session:
        repo = deps.keyword_repo(session)
        for raw in words:
            word = normalize_keyword(raw)
            if len(word) > deps.max_keyword_chars:
                too_long.append(word)
            elif await repo.add(word, type_) is None or word in added:
                duplicates.append(word)
            else:
                added.append(word)
    return AddResult(added=added, duplicates=duplicates, too_long=too_long)


def format_result(result: AddResult, max_chars: int) -> str:
    """Ответ: что добавлено и что пропущено."""
    lines = [f"Добавлено: {', '.join(result.added) if result.added else 'ничего'}"]
    if result.duplicates:
        lines.append(f"Пропущено (уже есть): {', '.join(result.duplicates)}")
    if result.too_long:
        lines.append(f"Отклонено (длиннее {max_chars} символов): {', '.join(result.too_long)}")
    return html.escape("\n".join(lines))


async def cmd_keywords(message: Any, keywords_deps: KeywordsDeps) -> None:
    """Показывает списки слов."""
    text, keyboard = await render_keywords(keywords_deps)
    await message.answer(text, reply_markup=keyboard)


async def on_delete(
    callback: Any, callback_data: KeywordAction, keywords_deps: KeywordsDeps
) -> None:
    """Удаление слова по кнопке."""
    async with keywords_deps.session_scope() as session:
        deleted = await keywords_deps.keyword_repo(session).delete(callback_data.id)
    if not deleted:
        await callback.answer(texts.KW_ALREADY_DELETED, show_alert=True)
    else:
        user_id = callback.from_user.id if callback.from_user else None
        log.info("bot.keyword_deleted", keyword_id=callback_data.id, user_id=user_id)
        await callback.answer(texts.KW_DELETED)
    text, keyboard = await render_keywords(keywords_deps)
    await callback.message.edit_text(text, reply_markup=keyboard)


async def on_add(callback: Any) -> None:
    """Кнопка «Добавить»: выбор типа слов."""
    builder = InlineKeyboardBuilder()
    for type_ in KeywordType:
        builder.button(
            text=texts.KW_TYPE_LABELS[type_.value],
            callback_data=KeywordAction(action=KW_TYPE, value=type_.value),
        )
    await callback.answer()
    await callback.message.answer(texts.KW_ASK_TYPE, reply_markup=builder.as_markup())


async def on_type(callback: Any, callback_data: KeywordAction, state: FSMContext) -> None:
    """Выбран тип слов: ждём ввод."""
    await state.set_state(AddKeywords.words)
    await state.update_data(type=KeywordType(callback_data.value).value)
    await callback.answer()
    await callback.message.answer(texts.KW_ASK_WORDS)


async def on_words(message: Any, state: FSMContext, keywords_deps: KeywordsDeps) -> None:
    """Ввод слов через запятую."""
    words = split_words(message.text or "")
    if not words:
        await message.answer(texts.KW_EMPTY_INPUT)
        return
    type_ = KeywordType((await state.get_data())["type"])
    result = await add_words(keywords_deps, words, type_)
    await state.clear()
    user_id = message.from_user.id if message.from_user else None
    log.info("bot.keywords_added", type=type_.value, added=len(result.added), user_id=user_id)
    await message.answer(format_result(result, keywords_deps.max_keyword_chars))
    text, keyboard = await render_keywords(keywords_deps)
    await message.answer(text, reply_markup=keyboard)


def create_router() -> Router:
    """Роутер управления словами."""
    router = Router(name="keywords")
    router.message.register(cmd_keywords, Command("keywords"))
    router.callback_query.register(on_delete, KeywordAction.filter(F.action == KW_DELETE))
    router.callback_query.register(on_add, KeywordAction.filter(F.action == KW_ADD))
    router.callback_query.register(on_type, KeywordAction.filter(F.action == KW_TYPE))
    router.message.register(on_words, AddKeywords.words)
    return router
