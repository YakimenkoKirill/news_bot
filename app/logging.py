"""JSON-логирование через structlog и stdlib logging."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import structlog
from pydantic import SecretStr
from structlog.typing import EventDict, Processor, WrappedLogger

MASK = "***"
SECRET_KEY_PARTS = frozenset(
    {"token", "tokens", "key", "keys", "secret", "secrets", "hash", "hashes", "password"}
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATOR = re.compile(r"[^a-z0-9]+")


class _AppHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Обработчик stdout, который ставит setup_logging; по типу его находят при перенастройке."""


def _is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = _CAMEL_BOUNDARY.sub("_", key).lower()
    return any(part in SECRET_KEY_PARTS for part in _KEY_SEPARATOR.split(normalized))


def _mask(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return MASK
    if isinstance(value, Mapping):
        return {k: MASK if _is_secret_key(k) else _mask(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_mask(item) for item in value]
    return value


def mask_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Заменяет секреты на «***».

    Маскируются значения SecretStr и значения ключей, в имени которых есть часть
    token, key, secret, hash или password (без учёта регистра), на любой глубине
    вложенных словарей и списков.

    Args:
        _logger: Обёрнутый логгер (не используется).
        _method_name: Имя метода логирования (не используется).
        event_dict: Событие structlog.

    Returns:
        Событие с замаскированными секретами.
    """
    return {k: MASK if _is_secret_key(k) else _mask(v) for k, v in event_dict.items()}


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        mask_secrets,
    ]


def setup_logging(level: str, json_output: bool) -> None:
    """Настраивает structlog и stdlib logging на вывод в stdout.

    Повторный вызов заменяет ранее установленный обработчик, а не добавляет второй.

    Args:
        level: Уровень логирования (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        json_output: True — одна JSON-строка на событие, False — читаемый вывод для консоли.
    """
    shared = _shared_processors()
    renderer: Processor = (
        structlog.processors.JSONRenderer(json.dumps, default=repr, ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    handler = _AppHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )

    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, _AppHandler)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


@contextmanager
def bind_context(**fields: Any) -> Iterator[None]:
    """Добавляет поля (source_id, news_id, job и др.) во все логи внутри блока.

    Args:
        **fields: Поля контекста; после выхода из блока восстанавливается прежний контекст.

    Yields:
        None.
    """
    with structlog.contextvars.bound_contextvars(**fields):
        yield
