"""Тексты бота управления."""

from datetime import datetime

ACCESS_DENIED = "Этот бот доступен только администраторам канала."

COMMANDS: dict[str, str] = {
    "start": "приветствие и меню",
    "help": "список команд",
    "status": "состояние пайплайна",
    "queue": "подготовленные посты, публикация вне очереди",
    "errors": "журнал ошибок, упавшие новости",
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
    "ai_item_failed": "🟠 <b>Модель не смогла обработать новость</b>\n{message}",
}

QUEUE_TITLES = {"queue": "<b>Очередь публикации</b>", "review": "<b>На модерации</b>"}
QUEUE_EMPTY = {"queue": "Очередь пуста.", "review": "На модерации ничего нет."}
QUEUE_HINT = (
    "Порядок — по интересу с поправкой на свежесть; новые интересные посты могут встать выше"
)
QUEUE_PAUSED = "⏸ Пайплайн на паузе — очередь стоит"
QUEUE_NOT_IN_QUEUE = "Пост уже не в очереди: {status}"
QUEUE_DELETED = "удалён"
QUEUE_REVIEW_WARNING = " ⚠️"


def queue_header(tab: str, count: int, page: int, pages: int) -> str:
    """Заголовок страницы /queue: вкладка, число постов, страница."""
    return f"{QUEUE_TITLES[tab]} · {count} постов · страница {page} из {pages}"


def queue_eta_text(eta: datetime, today: datetime) -> str:
    """Ожидаемое время выхода: «≈ЧЧ:ММ» сегодня, иначе «≈ДД.ММ ЧЧ:ММ» (оба в поясе канала)."""
    if eta.date() == today.date():
        return f"≈{eta:%H:%M}"
    return f"≈{eta:%d.%m %H:%M}"


def queue_line(number: int, title: str, source: str, score: float, eta: str | None) -> str:
    """Строка вкладки «В очереди»; title и source уже экранированы."""
    line = f"{number}. {title} — {source} · ★{score:.2f}"
    return f"{line} · {eta}" if eta else line


def review_line(number: int, title: str, source: str, score: float, has_note: bool) -> str:
    """Строка вкладки «На модерации»; ⚠️ — у поста есть замечание проверки фактов."""
    warning = QUEUE_REVIEW_WARNING if has_note else ""
    return f"{number}. {title} — {source} · ★{score:.2f}{warning}"


QUEUE_CONFIRM = (
    "Опубликовать сейчас, вне очереди? Интервал между постами и тихие часы не учитываются."
)
QUEUE_CONFIRM_PAUSED = "Пайплайн на паузе — пост всё равно выйдет."
QUEUE_PUBLISHING = "Публикую…"
QUEUE_PUBLISHED = "🚀 Опубликовано вне очереди"
QUEUE_PUBLISH_BUSY = "Пост сейчас публикуется — обновите список"
QUEUE_PUBLISH_DONE = "Уже обработано: {status}"
QUEUE_PUBLISH_NOT_FOUND = "Пост не найден"
QUEUE_PUBLISH_FAILED = "❌ Не удалось опубликовать: {error}"

ALERT_KIND_LABELS = {
    "source_down": "источник не отвечает",
    "source_stale": "источник молчит",
    "ai_down": "модель недоступна",
    "budget_exceeded": "бюджет исчерпан",
    "publish_error": "ошибка публикации",
    "job_failed": "сбой фоновой задачи",
    "ai_item_failed": "новость не обработана",
}

ERRORS_TITLE = "<b>Ошибки</b> · открыто {count}"
ERRORS_NEWS_TITLE = "<b>Новости с ошибкой</b> · {count}"
ERRORS_PAGE = "страница {page} из {pages}"
ERRORS_EMPTY = "Ошибок нет ✅"
ERRORS_NEWS_EMPTY = "Новостей с ошибкой нет ✅"
ERRORS_MUTED_MARK = "🔕 не присылается в чат"
ERRORS_ALREADY_RESOLVED = "Уже решено"
ERRORS_RESOLVED_COUNT = "Закрыто: {count}"
ERRORS_CONFIRM_ALL = "Закрыть все открытые ошибки?"
ERRORS_KIND_FROM_CONFIG = "Вид отключён в configs (alerts.silent_kinds)"
ERRORS_INCIDENT_NOT_FOUND = "Инцидент не найден"
ERRORS_OPEN_STATUS = "открыта"
ERRORS_RESOLVED_STATUS = "решена {who} {when}"

RETRY_REASONS = {
    "bad_ai_response": "модель вернула некорректный ответ",
    "publish_error": "не удалось опубликовать",
    "no_processed_post": "нет пересказа",
}
RETRY_HINT_TO_AI = "«Повторить» отправит новость в модель заново."
RETRY_HINT_TO_QUEUE = "«Повторить» вернёт пост в очередь публикации."
RETRY_RESULTS = {
    "to_ai": "Новость снова отправлена в модель",
    "to_queue": "Новость снова в очереди публикации",
    "not_failed": "Новость уже не в статусе failed: {status}",
    "not_found": "Новость не найдена",
}


def error_time(moment: datetime, today: datetime) -> str:
    """Время события: «ЧЧ:ММ» сегодня, иначе «ДД.ММ ЧЧ:ММ» (оба в поясе канала)."""
    if moment.date() == today.date():
        return f"{moment:%H:%M}"
    return f"{moment:%d.%m %H:%M}"


def kind_emoji(kind: str) -> str:
    """Эмодзи вида из шаблона алерта."""
    return ALERT_TEMPLATES[kind].split(" ", 1)[0]


def incident_line(number: int, kind: str, count: int, when: str, preview: str) -> str:
    """Строка списка инцидентов; preview уже экранирован и обрезан."""
    label = ALERT_KIND_LABELS[kind]
    return f"{number}. {kind_emoji(kind)} {label} ×{count} · {when} — {preview}"


def failed_news_line(number: int, title: str, source: str, reason: str) -> str:
    """Строка списка новостей с ошибкой; title и source уже экранированы."""
    return f"{number}. {title} — {source} · {RETRY_REASONS.get(reason, reason)}"


STATUS_ERRORS = "Открытых ошибок: {count} — /errors"
STATUS_NO_ERRORS = "Открытых ошибок нет"
