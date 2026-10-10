"""Машина состояний статусов новости.

Единственное место, где описаны допустимые переходы между статусами news_items.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.enums import NewsStatus

S = NewsStatus

ALLOWED_TRANSITIONS: Mapping[NewsStatus, frozenset[NewsStatus]] = {
    S.NEW: frozenset({S.DUPLICATE, S.FILTERED, S.NORMALIZED}),
    S.NORMALIZED: frozenset({S.PROCESSED, S.FILTERED, S.FAILED}),
    S.PROCESSED: frozenset({S.PENDING_REVIEW, S.APPROVED, S.FILTERED}),
    S.PENDING_REVIEW: frozenset({S.APPROVED, S.REJECTED}),
    S.APPROVED: frozenset({S.SCHEDULED, S.PUBLISHED, S.FAILED}),
    S.SCHEDULED: frozenset({S.PUBLISHED, S.FAILED}),
    S.FAILED: frozenset({S.APPROVED}),
    S.DUPLICATE: frozenset(),
    S.FILTERED: frozenset(),
    S.REJECTED: frozenset(),
    S.PUBLISHED: frozenset(),
}

TERMINAL_STATUSES = frozenset(status for status, nxt in ALLOWED_TRANSITIONS.items() if not nxt)


class InvalidTransition(ValueError):
    """Недопустимый переход между статусами новости."""

    def __init__(self, old: NewsStatus, new: NewsStatus) -> None:
        self.old = old
        self.new = new
        super().__init__(f"недопустимый переход статуса новости: {old.value} -> {new.value}")


def can_transition(old: NewsStatus, new: NewsStatus) -> bool:
    """Проверяет, разрешён ли переход old -> new."""
    return new in ALLOWED_TRANSITIONS[old]


def ensure_transition(old: NewsStatus, new: NewsStatus) -> None:
    """Проверяет переход old -> new.

    Raises:
        InvalidTransition: Переход не разрешён (в том числе в тот же статус).
    """
    if not can_transition(old, new):
        raise InvalidTransition(old, new)
