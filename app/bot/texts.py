"""Тексты бота управления."""

ACCESS_DENIED = "Этот бот доступен только администраторам канала."

COMMANDS: dict[str, str] = {
    "start": "приветствие и меню",
    "help": "список команд",
    "status": "состояние пайплайна",
    "pause": "приостановить обработку и публикацию",
    "resume": "возобновить работу",
    "sources": "источники: включить, выключить, удалить, проверить",
    "add_source": "добавить источник",
    "keywords": "ключевые и стоп-слова",
    "cancel": "отменить текущий диалог",
}

PAUSED = "Пайплайн поставлен на паузу: обработка моделью и публикация остановлены."
ALREADY_PAUSED = "Пайплайн уже на паузе."
RESUMED = "Работа возобновлена."
NOT_PAUSED = "Пайплайн и так работает, паузы нет."
NEVER = "нет"

START = (
    "Привет! Я бот управления новостным каналом.\n"
    "Через меню можно смотреть состояние, управлять источниками и модерацией.\n"
    "Список команд — /help."
)

UNKNOWN_COMMAND = "Не знаю такой команды. Список команд — /help."


def help_text() -> str:
    """Список команд с описаниями (тот же, что в меню Telegram)."""
    lines = ["<b>Команды</b>"]
    lines.extend(f"/{command} — {description}" for command, description in COMMANDS.items())
    return "\n".join(lines)


NO_SOURCES = "Источников пока нет. Добавьте первый: /add_source"
SOURCE_NOT_FOUND = "Источник не найден — возможно, его уже удалили."
SOURCE_DELETED = "Источник удалён."
CHECK_UNSUPPORTED = "Пробный сбор для Telegram-каналов не поддерживается: они собираются событиями."


def confirm_delete(name: str) -> str:
    """Вопрос перед удалением источника."""
    return f"Удалить источник «{name}» вместе с его новостями?"


ADD_ASK_TYPE = "Какой источник добавить?"
ADD_ASK_URL = {
    "rss": "Пришлите адрес RSS- или Atom-ленты (http:// или https://).",
    "web": "Пришлите адрес страницы со списком статей (http:// или https://).",
    "tg": "Пришлите публичный канал: @username или t.me/username.",
}
ADD_BAD_URL = "Нужен адрес вида https://… Попробуйте ещё раз."
ADD_BAD_TG = (
    "Нужен публичный канал: @username или t.me/username (5–32 символа). Попробуйте ещё раз."
)
ADD_DUPLICATE = "Такой источник уже есть. Диалог завершён."
ADD_ASK_NAME = "Как назвать источник? Это имя видно в подписи к постам."
ADD_BAD_NAME = "Имя не может быть пустым или длиннее {max_len} символов. Попробуйте ещё раз."
ADD_ASK_INTERVAL = "Как часто проверять источник? Число минут, не меньше {min_value}."
ADD_BAD_INTERVAL = "Нужно целое число минут, не меньше {min_value}. Попробуйте ещё раз."
ADD_ASK_MODERATION = "Публиковать сразу или сначала отправлять на модерацию?"
ADD_ASK_SELECTORS = (
    "Пришлите CSS-селекторы в JSON, например:\n"
    '<code>{"list_item": "article.news", "link": "a", "title": "h2"}</code>'
)
ADD_BAD_SELECTORS = "Селекторы не подошли: {reason}. Попробуйте ещё раз."
ADD_SAVED = "Источник #{source_id} «{name}» сохранён."
ADD_CANCELLED = "Диалог отменён."
NOTHING_TO_CANCEL = "Отменять нечего."
CHECK_EMPTY = "Пробный сбор: материалов не найдено."

KW_TITLE_INCLUDE = "<b>Ключевые слова</b> (новость должна содержать хотя бы одно)"
KW_TITLE_EXCLUDE = "<b>Стоп-слова</b> (новость с ними отбрасывается)"
KW_EMPTY = "— пусто"
KW_ASK_TYPE = "Какие слова добавить?"
KW_ASK_WORDS = "Пришлите слова или фразы через запятую."
KW_EMPTY_INPUT = "Не нашёл ни одного слова. Пришлите слова через запятую."
KW_ALREADY_DELETED = "Это слово уже удалено."
KW_DELETED = "Слово удалено."
KW_TYPE_LABELS = {"include": "Ключевые", "exclude": "Стоп-слова"}

REVIEW_POST_NOT_FOUND = "Пост не найден."
REVIEW_ALREADY_DONE = "Уже обработано: {status}"
REVIEW_ASK_SUMMARY = "Пришлите новый текст пересказа (не длиннее {limit} символов)."
REVIEW_SUMMARY_TOO_LONG = "Текст длиннее {limit} символов. Сократите и пришлите ещё раз."
REVIEW_SUMMARY_EMPTY = "Текст пустой. Пришлите новый пересказ."
REVIEW_EDITED = "Пересказ обновлён, пост одобрен к публикации."
REVIEW_DECISIONS = {
    "publish": "✅ Одобрено к публикации",
    "reject": "🚫 Отклонено",
    "later": "⏰ Отложено на {minutes} мин",
    "edit": "✏️ Отредактировано и одобрено",
}

ALERT_TEMPLATES = {
    "source_down": "🔴 <b>Источник не отвечает</b>\n{message}",
    "source_stale": "🟡 <b>Источник молчит</b>\n{message}",
    "ai_down": "🔴 <b>Модель недоступна</b>\n{message}",
    "budget_exceeded": "💸 <b>Бюджет на модель исчерпан</b>\n{message}",
    "publish_error": "🔴 <b>Ошибка публикации</b>\n{message}",
    "job_failed": "⚠️ <b>Ошибка фоновой задачи</b>\n{message}",
}
