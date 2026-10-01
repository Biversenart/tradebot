"""Clock abstraction so live, paper and backtest share the same code paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Wall clock in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Deterministic clock for tests and backtests."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock zaman dilimi bilgisi olan bir datetime ister.")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("Saat geriye alınamaz.")
        self._now += delta

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("Zaman dilimi bilgisi zorunlu.")
        if value < self._now:
            raise ValueError("Saat geriye alınamaz.")
        self._now = value
