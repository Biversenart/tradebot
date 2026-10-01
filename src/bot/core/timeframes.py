"""Timeframe helpers (`1m`, `5m`, `15m`, `1h`, `4h`, `1d`, `1w`). All times are UTC."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
_RE = re.compile(r"^(\d+)([mhdw])$")
# Binance weekly candles open on Monday 00:00 UTC; 1970-01-05 was a Monday.
_WEEK_ANCHOR = datetime(1970, 1, 5, tzinfo=UTC)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def timeframe_seconds(tf: str) -> int:
    m = _RE.match(tf)
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"Geçersiz zaman dilimi: {tf!r}")
    return int(m.group(1)) * _UNITS[m.group(2)]


def timeframe_delta(tf: str) -> timedelta:
    return timedelta(seconds=timeframe_seconds(tf))


def floor_time(ts: datetime, tf: str) -> datetime:
    """Start of the candle that contains `ts`."""
    if ts.tzinfo is None:
        raise ValueError("Zaman dilimi bilgisi zorunlu.")
    seconds = timeframe_seconds(tf)
    anchor = _WEEK_ANCHOR if tf.endswith("w") else _EPOCH
    elapsed = int((ts.astimezone(UTC) - anchor).total_seconds())
    return anchor + timedelta(seconds=elapsed - elapsed % seconds)


def to_ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def from_ms(ms: int | float) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
