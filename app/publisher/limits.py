"""Подгонка поста под лимиты длины Telegram."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from app.config import PublisherConfig
from app.publisher.formatter import Post

ELLIPSIS = "…"
SENTENCE_ENDS = ".!?…"
_TAG = re.compile(r"<[^>]+>")
_UTF16 = "utf-16-le"
_UTF16_UNIT_BYTES = 2
_PROBE = "x"


def utf16_len(text: str) -> int:
    """Длина в единицах UTF-16 (так Telegram считает лимиты; эмодзи — 2)."""
    return len(text.encode(_UTF16)) // _UTF16_UNIT_BYTES


def visible_length(html_text: str) -> int:
    """Длина видимого текста HTML-поста в единицах UTF-16 (без тегов, с раскрытыми сущностями)."""
    return utf16_len(html.unescape(_TAG.sub("", html_text)))


@dataclass(frozen=True)
class FittedPost:
    """Пост, уложенный в лимит."""

    html: str
    use_photo: bool


def _prefix_within(text: str, limit: int) -> str:
    """Самый длинный префикс text, который занимает не больше limit единиц UTF-16."""
    total = 0
    for index, char in enumerate(text):
        total += utf16_len(char)
        if total > limit:
            return text[:index]
    return text


def shorten_summary(summary: str, limit: int) -> str:
    """Сокращает пересказ до limit единиц UTF-16.

    Сначала по концу последнего помещающегося предложения, иначе по границе слова
    с «…»; слово без пробелов обрезается жёстко с «…».

    Args:
        summary: Пересказ.
        limit: Допустимая длина в единицах UTF-16.

    Returns:
        Пересказ не длиннее limit (пустой, если limit не вмещает даже «…»).
    """
    if utf16_len(summary) <= limit:
        return summary
    if limit <= utf16_len(ELLIPSIS):
        return ""
    prefix = _prefix_within(summary, limit)
    for index in range(len(prefix) - 1, -1, -1):
        if prefix[index] in SENTENCE_ENDS and (
            index + 1 == len(summary) or summary[index + 1].isspace()
        ):
            sentence = prefix[: index + 1].rstrip()
            if sentence:
                return sentence
    word_prefix = _prefix_within(summary, limit - utf16_len(ELLIPSIS))
    boundary = word_prefix.rfind(" ")
    if boundary > 0 and not summary[len(word_prefix)].isspace():
        word_prefix = word_prefix[:boundary]
    return word_prefix.rstrip(" ,;:—-") + ELLIPSIS


def _summary_budget(post: Post, limit: int) -> int:
    overhead = visible_length(post.with_summary(_PROBE).html) - utf16_len(_PROBE)
    return limit - overhead


def _fit_to(post: Post, limit: int) -> str:
    if visible_length(post.html) <= limit:
        return post.html
    return post.with_summary(shorten_summary(post.summary, _summary_budget(post, limit))).html


def fit_post(post: Post, has_photo: bool, cfg: PublisherConfig) -> FittedPost:
    """Укладывает пост в лимит подписи к фото или текстового сообщения.

    Сокращается только пересказ; заголовок, хэштеги и ссылка не меняются. Если
    с фото не помещается даже min_summary_chars пересказа, пост отправляется
    без фото с лимитом текстового сообщения.

    Args:
        post: Собранный пост.
        has_photo: Есть ли у поста подходящая картинка.
        cfg: Секция publisher.

    Returns:
        HTML поста и признак отправки с фото.
    """
    if has_photo:
        fits_whole = visible_length(post.html) <= cfg.caption_limit
        budget = _summary_budget(post, cfg.caption_limit)
        if fits_whole or budget >= min(cfg.min_summary_chars, utf16_len(post.summary)):
            return FittedPost(html=_fit_to(post, cfg.caption_limit), use_photo=True)
    return FittedPost(html=_fit_to(post, cfg.text_limit), use_photo=False)
