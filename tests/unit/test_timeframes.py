from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.core.timeframes import floor_time, from_ms, timeframe_delta, timeframe_seconds, to_ms


@pytest.mark.parametrize(
    ("tf", "secs"),
    [
        ("1m", 60),
        ("5m", 300),
        ("15m", 900),
        ("1h", 3600),
        ("4h", 14400),
        ("1d", 86400),
        ("1w", 604800),
    ],
)
def test_seconds(tf: str, secs: int) -> None:
    assert timeframe_seconds(tf) == secs
    assert timeframe_delta(tf) == timedelta(seconds=secs)


@pytest.mark.parametrize("tf", ["", "1x", "0m", "h1", "1.5h"])
def test_invalid(tf: str) -> None:
    with pytest.raises(ValueError):
        timeframe_seconds(tf)


def test_floor() -> None:
    ts = datetime(2024, 3, 7, 13, 47, 12, tzinfo=UTC)  # Thursday
    assert floor_time(ts, "15m") == datetime(2024, 3, 7, 13, 45, tzinfo=UTC)
    assert floor_time(ts, "4h") == datetime(2024, 3, 7, 12, tzinfo=UTC)
    assert floor_time(ts, "1d") == datetime(2024, 3, 7, tzinfo=UTC)
    assert floor_time(ts, "1w") == datetime(2024, 3, 4, tzinfo=UTC)  # Monday
    with pytest.raises(ValueError):
        floor_time(datetime(2024, 1, 1), "1h")


def test_ms_roundtrip() -> None:
    ts = datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert to_ms(ts) == 1704067201000
    assert from_ms(to_ms(ts)) == ts
