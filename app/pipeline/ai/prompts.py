"""Версионируемый промпт пересказа новости для канала."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.config import ChannelProfile

PROMPT_VERSION = "v3"
MAX_QUOTE_WORDS = 15
ELLIPSIS = "…"
RESPONSE_KEYS = ("relevant", "score", "title", "summary", "hashtags", "category")

__all__ = ["PROMPT_VERSION", "RESPONSE_KEYS", "ChannelProfile", "build_messages", "truncate_words"]


class PromptNews(Protocol):
    """Поля новости для промпта (NewsItem подходит структурно)."""

    title: str
    clean_text: str | None
    raw_text: str
    published_at: datetime | None


def truncate_words(text: str, max_chars: int) -> str:
    """Обрезает текст до max_chars символов по границе слова, добавляя «…».

    Если в пределах лимита нет пробела, текст обрезается ровно по лимиту.
    """
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - len(ELLIPSIS)]
    boundary = cut.rfind(" ")
    if boundary > 0 and not text[len(cut)].isspace():
        cut = cut[:boundary]
    return cut.rstrip() + ELLIPSIS


def _hashtags_rule(profile: ChannelProfile) -> str:
    if profile.hashtags_max == 0:
        return '- "hashtags": пустой список, хэштеги в канале не используются;\n'
    return f'- "hashtags": список не больше чем из {profile.hashtags_max} хэштегов;\n'


def _system_message(profile: ChannelProfile) -> str:
    return (
        f"Ты — редактор Telegram-канала. Тематика канала: {profile.topic}. "
        f"Пиши на языке «{profile.language}», тон: {profile.tone}.\n"
        "Тебе дают одну новость из внешнего источника. Задачи:\n"
        "1. Оцени, подходит ли новость тематике канала.\n"
        "2. Перескажи её своими словами: коротко, по существу, без оценок от себя.\n"
        "Правила:\n"
        "- Не добавляй факты, цифры, имена и даты, которых нет в источнике.\n"
        f"- Не цитируй источник дословно фрагментами длиннее {MAX_QUOTE_WORDS} слов.\n"
        "- Не используй кликбейт, эмодзи и призывы подписаться.\n"
        "Ответь ровно одним JSON-объектом с ключами "
        f"{', '.join(RESPONSE_KEYS)} и без других ключей:\n"
        '- "relevant": true или false — подходит ли новость тематике;\n'
        '- "score": число от 0 до 1 — насколько новость важна и интересна читателям канала;\n'
        f'- "title": заголовок не длиннее {profile.max_title_chars} символов;\n'
        f'- "summary": пересказ не длиннее {profile.max_summary_chars} символов;\n'
        + _hashtags_rule(profile)
        + '- "category": одно слово — рубрика новости.'
    )


def build_messages(
    news: PromptNews, profile: ChannelProfile, max_input_chars: int
) -> list[dict[str, str]]:
    """Собирает сообщения для модели.

    Системное сообщение содержит только правила и профиль канала; заголовок,
    дата и текст новости — только в пользовательском сообщении. Источник модели
    не передаётся: ссылку на него добавляет публикатор.

    Args:
        news: Новость.
        profile: Профиль канала (секция channel).
        max_input_chars: Максимальная длина текста новости (ai.max_input_chars).

    Returns:
        Сообщения в формате OpenAI: system и user.
    """
    published = news.published_at.isoformat() if news.published_at else "не указана"
    text = truncate_words(news.clean_text or news.raw_text, max_input_chars)
    user = f"Заголовок: {news.title}\nДата публикации: {published}\nТекст:\n{text}"
    return [
        {"role": "system", "content": _system_message(profile)},
        {"role": "user", "content": user},
    ]
