from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
import structlog

from app.config import AiConfig, ChannelProfile, PublisherConfig
from app.db.repositories.news import Priority, QueuedRef
from app.domain.enums import NewsStatus
from app.domain.status import InvalidTransition, ensure_transition
from app.pipeline.queue_limit import (
    QUEUE_STATUSES,
    REASON_QUEUE_LIMIT,
    AdmitOutcome,
    AdmitResult,
    QueueAdmission,
    QueueLimitRepos,
    admit_to_queue,
    trim_queue,
)
from app.runtime_config import RuntimeSnapshot
from tests.config_helpers import base_section

S = NewsStatus
SOURCE = 1
OTHER_SOURCE = 2
NEWS = 100
DECAY = 0.02
PRIORITY = Priority(DECAY)
MAX = 3


@dataclass
class Item:
    news_id: int
    source_id: int
    status: NewsStatus
    priority: float | None


@dataclass
class Db:
    items: dict[int, Item] = field(default_factory=dict)
    locked: list[int] = field(default_factory=list)
    reasons: dict[int, str] = field(default_factory=dict)

    def add(
        self,
        news_id: int,
        priority: float | None,
        status: NewsStatus = S.APPROVED,
        source_id: int = SOURCE,
    ) -> int:
        self.items[news_id] = Item(news_id, source_id, status, priority)
        return news_id


class NewsRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def set_status(
        self, news_id: int, expected: NewsStatus, new: NewsStatus, reason: str | None = None
    ) -> None:
        item = self.db.items[news_id]
        assert item.status == expected, f"ожидался {expected}, был {item.status}"
        ensure_transition(expected, new)
        item.status = new
        if reason:
            self.db.reasons[news_id] = reason

    async def queued_by_source(
        self, source_id: int, statuses: Sequence[NewsStatus], priority: Priority
    ) -> list[QueuedRef]:
        queued = [
            i
            for i in self.db.items.values()
            if i.source_id == source_id and i.status in statuses and i.priority is not None
        ]
        queued.sort(key=lambda i: (i.priority, -i.news_id))
        return [
            QueuedRef(news_id=i.news_id, status=i.status, priority=float(i.priority or 0))
            for i in queued
        ]

    async def priority_of(self, news_id: int, priority: Priority) -> float | None:
        item = self.db.items.get(news_id)
        return item.priority if item else None

    async def queue_counts_by_source(self, statuses: Sequence[NewsStatus]) -> dict[int, int]:
        counts: dict[int, int] = {}
        for item in self.db.items.values():
            if item.status in statuses and item.priority is not None:
                counts[item.source_id] = counts.get(item.source_id, 0) + 1
        return counts


class SourceRepo:
    def __init__(self, db: Db) -> None:
        self.db = db

    async def lock(self, source_id: int) -> bool:
        self.db.locked.append(source_id)
        return True


def _repos(db: Db) -> QueueLimitRepos:
    return QueueLimitRepos(news=NewsRepo(db), sources=SourceRepo(db))


async def _admit(
    db: Db,
    news_id: int = NEWS,
    expected: NewsStatus = S.PROCESSED,
    max_per_source: int = MAX,
    forced: bool = False,
) -> AdmitResult:
    return await admit_to_queue(
        news_id, SOURCE, expected, _repos(db), max_per_source, PRIORITY, forced=forced
    )


def _queue(db: Db, source_id: int = SOURCE) -> set[int]:
    return {
        i.news_id
        for i in db.items.values()
        if i.source_id == source_id and i.status in QUEUE_STATUSES
    }


async def test_empty_queue_admits() -> None:
    db = Db()
    db.add(NEWS, 0.5, status=S.PROCESSED)
    result = await _admit(db)
    assert result == AdmitResult(AdmitOutcome.ADMITTED, S.APPROVED)
    assert db.items[NEWS].status is S.APPROVED
    assert db.locked == [SOURCE]


async def test_one_slot_left_admits() -> None:
    db = Db()
    for index in range(MAX - 1):
        db.add(index + 1, 0.9)
    db.add(NEWS, 0.1, status=S.PROCESSED)
    assert (await _admit(db)).outcome is AdmitOutcome.ADMITTED
    assert len(_queue(db)) == MAX


async def test_stronger_displaces_weakest() -> None:
    db = Db()
    weak = db.add(1, 0.2)
    db.add(2, 0.6)
    db.add(3, 0.7)
    db.add(NEWS, 0.9, status=S.PROCESSED)

    result = await _admit(db)
    assert result == AdmitResult(AdmitOutcome.ADMITTED_DISPLACED, S.APPROVED, displaced_id=weak)
    assert db.items[weak].status is S.FILTERED
    assert db.reasons[weak] == REASON_QUEUE_LIMIT
    assert _queue(db) == {2, 3, NEWS}


async def test_weaker_is_rejected() -> None:
    db = Db()
    for index, score in enumerate([0.5, 0.6, 0.7], start=1):
        db.add(index, score)
    db.add(NEWS, 0.4, status=S.PROCESSED)

    result = await _admit(db)
    assert result == AdmitResult(AdmitOutcome.REJECTED, S.FILTERED)
    assert db.items[NEWS].status is S.FILTERED
    assert db.reasons[NEWS] == REASON_QUEUE_LIMIT
    assert _queue(db) == {1, 2, 3}


async def test_equal_priority_is_rejected() -> None:
    db = Db()
    for index in range(1, MAX + 1):
        db.add(index, 0.5)
    db.add(NEWS, 0.5, status=S.PROCESSED)
    assert (await _admit(db)).outcome is AdmitOutcome.REJECTED


async def test_forced_admits_even_when_weaker() -> None:
    db = Db()
    weak = db.add(1, 0.5)
    db.add(2, 0.6)
    db.add(3, 0.7)
    db.add(NEWS, 0.1, status=S.PENDING_REVIEW)

    result = await _admit(db, expected=S.PENDING_REVIEW, forced=True)
    assert result.outcome is AdmitOutcome.ADMITTED_DISPLACED
    assert result.displaced_id == weak
    assert db.items[NEWS].status is S.APPROVED


async def test_scheduled_post_can_be_displaced() -> None:
    db = Db()
    weak = db.add(1, 0.2, status=S.SCHEDULED)
    db.add(2, 0.6)
    db.add(3, 0.7)
    db.add(NEWS, 0.9, status=S.PROCESSED)

    assert (await _admit(db)).displaced_id == weak
    assert db.items[weak].status is S.FILTERED


async def test_other_sources_not_counted() -> None:
    db = Db()
    for index in range(1, MAX + 1):
        db.add(index, 0.9, source_id=OTHER_SOURCE)
    db.add(NEWS, 0.1, status=S.PROCESSED)
    assert (await _admit(db)).outcome is AdmitOutcome.ADMITTED


async def test_zero_limit_skips_checks() -> None:
    db = Db()
    for index in range(1, 10):
        db.add(index, 0.9)
    db.add(NEWS, 0.1, status=S.PROCESSED)

    result = await _admit(db, max_per_source=0)
    assert result == AdmitResult(AdmitOutcome.ADMITTED, S.APPROVED)
    assert db.locked == [], "источник не блокируется — лимита нет"
    assert len(_queue(db)) == 10


async def test_overfilled_queue_displaces_one() -> None:
    db = Db()
    weak = db.add(1, 0.1)
    for index, score in enumerate([0.2, 0.3, 0.4, 0.5], start=2):
        db.add(index, score)
    db.add(NEWS, 0.9, status=S.PROCESSED)

    result = await _admit(db, max_per_source=2)
    assert result.displaced_id == weak
    assert len(_queue(db)) == 5, "остальное лишнее уберёт trim_queue"


@pytest.mark.parametrize("expected", [S.PENDING_REVIEW, S.FAILED, S.SCHEDULED])
async def test_not_forced_from_other_status_rejected(expected: NewsStatus) -> None:
    db = Db()
    db.add(NEWS, 0.5, status=expected)
    with pytest.raises(ValueError, match="processed"):
        await _admit(db, expected=expected)


async def test_news_without_post_rejected() -> None:
    db = Db()
    for index in range(1, MAX + 1):
        db.add(index, 0.5)
    db.add(NEWS, None, status=S.PROCESSED)
    with pytest.raises(ValueError, match="пересказа"):
        await _admit(db)


async def test_outcome_is_logged() -> None:
    db = Db()
    db.add(1, 0.2)
    db.add(2, 0.6)
    db.add(3, 0.7)
    db.add(NEWS, 0.9, status=S.PROCESSED)
    with structlog.testing.capture_logs() as logs:
        await _admit(db)
    [entry] = [e for e in logs if e["event"] == "queue_limit.admit"]
    assert entry["outcome"] == "admitted_displaced"
    assert entry["displaced_id"] == 1
    assert entry["priority"] == 0.9
    assert entry["weakest_priority"] == 0.2


async def test_invalid_transition_still_guarded() -> None:
    """Машина состояний остаётся главной: published в очередь не вернуть."""
    db = Db()
    db.add(NEWS, 0.5, status=S.PUBLISHED)
    with pytest.raises(InvalidTransition):
        await _admit(db, expected=S.PUBLISHED, forced=True)


async def test_trim_removes_extra_posts() -> None:
    db = Db()
    for index, score in enumerate([0.1, 0.2, 0.3, 0.9, 0.8], start=1):
        db.add(index, score)

    displaced = await trim_queue(_repos(db), 2, PRIORITY)
    assert displaced == 3
    assert _queue(db) == {4, 5}, "остались сильнейшие"
    assert all(db.reasons[i] == REASON_QUEUE_LIMIT for i in (1, 2, 3))


async def test_trim_touches_only_overfilled_sources() -> None:
    db = Db()
    db.add(1, 0.5)
    db.add(2, 0.6)
    for index, score in enumerate([0.1, 0.2, 0.3], start=10):
        db.add(index, score, source_id=OTHER_SOURCE)

    assert await trim_queue(_repos(db), 2, PRIORITY) == 1
    assert _queue(db) == {1, 2}
    assert _queue(db, OTHER_SOURCE) == {11, 12}
    assert db.locked == [OTHER_SOURCE]


async def test_trim_does_nothing_within_limit() -> None:
    db = Db()
    db.add(1, 0.5)
    db.add(2, 0.6)
    assert await trim_queue(_repos(db), 2, PRIORITY) == 0
    assert db.locked == []


async def test_trim_with_zero_limit_does_nothing() -> None:
    db = Db()
    for index in range(1, 5):
        db.add(index, 0.5)
    assert await trim_queue(_repos(db), 0, PRIORITY) == 0
    assert len(_queue(db)) == 4


class FakeRuntime:
    """Снимок изменяемых параметров: лимит источника и старение оценки."""

    def __init__(self, cfg: PublisherConfig) -> None:
        self.value = RuntimeSnapshot(
            channel=ChannelProfile(**base_section("channel")),
            ai=AiConfig(**base_section("ai")),
            publisher=cfg,
        )

    async def snapshot(self) -> RuntimeSnapshot:
        return self.value


def _admission(runtime: FakeRuntime | None = None) -> QueueAdmission:
    return QueueAdmission(
        news_repo=NewsRepo,
        source_repo=SourceRepo,
        max_per_source=4,
        priority=PRIORITY,
        runtime=runtime,
    )


async def test_limits_from_yaml_without_runtime() -> None:
    assert await _admission().limits() == (4, PRIORITY)


async def test_limits_from_runtime_snapshot() -> None:
    """Правка лимита и старения применяется без перезапуска."""
    cfg = PublisherConfig(
        **{**base_section("publisher"), "max_queued_per_source": 1, "score_decay_per_h": 0}
    )
    max_per_source, priority = await _admission(FakeRuntime(cfg)).limits()
    assert max_per_source == 1
    assert priority == Priority.from_config(cfg)


async def test_admit_uses_runtime_limit() -> None:
    """С лимитом 1 второй пост источника вытесняет слабейшего."""
    cfg = PublisherConfig(**{**base_section("publisher"), "max_queued_per_source": 1})
    db = Db()
    strong = db.add(1, priority=0.9, status=S.APPROVED)
    fresh = db.add(2, priority=0.95, status=S.PROCESSED)

    admission = QueueAdmission(
        news_repo=NewsRepo,
        source_repo=SourceRepo,
        max_per_source=99,
        priority=PRIORITY,
        runtime=FakeRuntime(cfg),
    )
    result = await admission.admit(db, fresh, SOURCE, S.PROCESSED)

    assert result.outcome is AdmitOutcome.ADMITTED_DISPLACED
    assert db.items[strong].status is S.FILTERED
    assert db.items[fresh].status is S.APPROVED
