"""Heartbeat: periodic ping to an external monitor (healthchecks.io, Uptime Kuma push, ...).

The external service alerts (e.g. via its Telegram integration) when pings stop. A ping is
sent only while the bot reports itself healthy, so an unhealthy bot (egress blocked, kill
switch active) also stops pinging and triggers the alert.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from pydantic import SecretStr

from bot.core.aio import wait_or_stop
from bot.log import get_logger

HealthCheck = Callable[[], bool]
Pinger = Callable[[str], Awaitable[int]]

_log = get_logger(__name__)


class Heartbeat:
    def __init__(
        self,
        url: SecretStr,
        interval: timedelta,
        ping: Pinger,
        is_healthy: HealthCheck = lambda: True,
    ) -> None:
        self._url = url
        self._interval = interval
        self._ping = ping
        self._is_healthy = is_healthy
        self.sent = 0
        self.skipped = 0
        self.failed = 0

    async def beat(self) -> bool:
        """Send one heartbeat if healthy. Returns True if a ping was delivered."""
        if not self._is_healthy():
            self.skipped += 1
            _log.warning("heartbeat_skipped_unhealthy")
            return False
        try:
            status = await self._ping(self._url.get_secret_value())
        except Exception as exc:
            self.failed += 1
            _log.warning("heartbeat_failed", error=type(exc).__name__)
            return False
        if status >= 400:
            self.failed += 1
            _log.warning("heartbeat_failed", status=status)
            return False
        self.sent += 1
        return True

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.beat()
            await wait_or_stop(stop, self._interval.total_seconds())
