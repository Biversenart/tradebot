from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.core.clock import ManualClock, SystemClock


def test_system_clock_is_utc_aware() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_manual_clock(now: datetime) -> None:
    c = ManualClock(now)
    c.advance(timedelta(minutes=5))
    assert c.now() == now + timedelta(minutes=5)
    c.set(now + timedelta(hours=1))
    assert c.now() == now + timedelta(hours=1)
    with pytest.raises(ValueError):
        c.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        c.set(now)
    with pytest.raises(ValueError):
        ManualClock(datetime(2026, 1, 1))
