import itertools

import pytest

from app.domain.enums import NewsStatus
from app.domain.status import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidTransition,
    can_transition,
    ensure_transition,
)

S = NewsStatus

EXPECTED = {
    (S.NEW, S.DUPLICATE),
    (S.NEW, S.FILTERED),
    (S.NEW, S.NORMALIZED),
    (S.NORMALIZED, S.PROCESSED),
    (S.NORMALIZED, S.FILTERED),
    (S.NORMALIZED, S.FAILED),
    (S.PROCESSED, S.PENDING_REVIEW),
    (S.PROCESSED, S.APPROVED),
    (S.PROCESSED, S.FILTERED),
    (S.PENDING_REVIEW, S.APPROVED),
    (S.PENDING_REVIEW, S.REJECTED),
    (S.APPROVED, S.SCHEDULED),
    (S.APPROVED, S.PUBLISHED),
    (S.APPROVED, S.FAILED),
    (S.SCHEDULED, S.PUBLISHED),
    (S.SCHEDULED, S.FAILED),
    (S.FAILED, S.APPROVED),
    (S.FAILED, S.NORMALIZED),
}
ALL_PAIRS = list(itertools.product(NewsStatus, repeat=2))


def test_every_status_has_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(NewsStatus)


@pytest.mark.parametrize(("old", "new"), ALL_PAIRS, ids=lambda s: s.value)
def test_full_matrix(old: NewsStatus, new: NewsStatus) -> None:
    allowed = (old, new) in EXPECTED
    assert can_transition(old, new) is allowed
    if allowed:
        ensure_transition(old, new)
    else:
        with pytest.raises(InvalidTransition):
            ensure_transition(old, new)


@pytest.mark.parametrize("status", list(NewsStatus), ids=lambda s: s.value)
def test_same_status_forbidden(status: NewsStatus) -> None:
    assert not can_transition(status, status)


def test_exception_message_contains_both_statuses() -> None:
    with pytest.raises(InvalidTransition) as caught:
        ensure_transition(S.PUBLISHED, S.NEW)
    assert caught.value.old is S.PUBLISHED
    assert caught.value.new is S.NEW
    assert "published" in str(caught.value)
    assert "new" in str(caught.value)
    assert isinstance(caught.value, ValueError)


def test_terminal_statuses() -> None:
    assert {S.DUPLICATE, S.FILTERED, S.REJECTED, S.PUBLISHED} == TERMINAL_STATUSES
    for status in TERMINAL_STATUSES:
        assert not any(can_transition(status, target) for target in NewsStatus)
