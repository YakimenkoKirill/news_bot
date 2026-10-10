"""Тексты бота управления."""

ACCESS_DENIED = "Этот бот доступен только администраторам канала."

COMMANDS: dict[str, str] = {
    "start": "приветствие и меню",
    "help": "список команд",
}

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
