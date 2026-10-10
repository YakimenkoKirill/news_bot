"""Проверка robots.txt с кэшем на домен."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import structlog

from app.collectors.base import PermanentCollectorError, TemporaryCollectorError
from app.collectors.http import Clock, HttpClient

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _Policy:
    """Правила одного домена: разобранный robots.txt или «всё разрешено/запрещено»."""

    parser: RobotFileParser | None
    allow_all: bool
    expires_at: float

    def allows(self, user_agent: str, url: str) -> bool:
        if self.parser is None:
            return self.allow_all
        return self.parser.can_fetch(user_agent, url)


class RobotsChecker:
    """Проверяет, разрешает ли robots.txt нашему User-Agent загрузку адреса.

    По RFC 9309: robots.txt с ответом 4xx — разрешено всё; 5xx или сайт
    недоступен — запрещено всё. Результат кэшируется на домен на ttl.
    """

    def __init__(
        self,
        http_client: HttpClient,
        user_agent: str,
        ttl: timedelta,
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        self._http = http_client
        self._user_agent = user_agent
        self._ttl = ttl.total_seconds()
        self._clock = clock
        self._cache: dict[str, _Policy] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def allowed(self, url: str) -> bool:
        """Возвращает True, если robots.txt домена разрешает загрузку url."""
        parts = urlsplit(url)
        origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            policy = self._cache.get(origin)
            if policy is None or policy.expires_at <= self._clock():
                policy = await self._load(origin)
                self._cache[origin] = policy
        return policy.allows(self._user_agent, url)

    async def _load(self, origin: str) -> _Policy:
        robots_url = f"{origin}/robots.txt"
        expires_at = self._clock() + self._ttl
        try:
            response = await self._http.get(robots_url)
        except PermanentCollectorError as exc:
            log.info("robots.unavailable_allow_all", url=robots_url, error=str(exc))
            return _Policy(parser=None, allow_all=True, expires_at=expires_at)
        except TemporaryCollectorError as exc:
            log.warning("robots.unreachable_disallow_all", url=robots_url, error=str(exc))
            return _Policy(parser=None, allow_all=False, expires_at=expires_at)
        parser = RobotFileParser(robots_url)
        parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
        return _Policy(parser=parser, allow_all=False, expires_at=expires_at)
