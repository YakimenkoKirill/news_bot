"""Отсев рекламных и пересланных сообщений каналов."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.config import TelegramConfig

FORWARDED_REASON = "forwarded"
AD_MARKER_REASON = "ad_marker"


def _marker_pattern(marker: str) -> re.Pattern[str]:
    """Маркер как отдельное слово: до и после не должно быть букв и цифр."""
    return re.compile(rf"(?<!\w){re.escape(marker)}(?!\w)", re.IGNORECASE)


@dataclass(frozen=True)
class AdRules:
    """Правила отсева.

    Attributes:
        markers: Маркеры рекламы (например, #реклама, erid), ищутся как отдельные слова.
        skip_forwards: Отсеивать пересланные сообщения.
    """

    markers: list[str]
    skip_forwards: bool
    _patterns: list[tuple[str, re.Pattern[str]]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        patterns = [(m, _marker_pattern(m)) for m in self.markers if m.strip()]
        object.__setattr__(self, "_patterns", patterns)

    @classmethod
    def from_config(cls, config: TelegramConfig) -> AdRules:
        """Правила из секции telegram."""
        return cls(markers=list(config.ad_markers), skip_forwards=config.skip_forwards)

    def find_marker(self, text: str) -> str | None:
        """Первый найденный маркер или None."""
        for marker, pattern in self._patterns:
            if pattern.search(text):
                return marker
        return None


def is_advertisement(message: Any, rules: AdRules) -> str | None:
    """Проверяет сообщение канала.

    Returns:
        Причина отсева («forwarded» или «ad_marker:<маркер>») либо None.
    """
    if rules.skip_forwards and getattr(message, "fwd_from", None) is not None:
        return FORWARDED_REASON
    marker = rules.find_marker(str(getattr(message, "message", "") or ""))
    if marker is not None:
        return f"{AD_MARKER_REASON}:{marker}"
    return None
