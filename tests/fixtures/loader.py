from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pandas as pd

FIXTURES = Path(__file__).parent


def synthetic_1h() -> pd.DataFrame:
    """1500 x 1h bars; segments: up 0-399, range 400-799, down 800-1199, volatile 1200-1499."""
    df = pd.read_csv(FIXTURES / "synthetic_1h.csv", index_col=0, parse_dates=True)
    idx = pd.DatetimeIndex(df.index)
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx
    return df


def frame(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float] | None = None,
    start: str = "2024-01-01",
    freq: str = "1h",
) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes or [1.0] * len(closes),
        },
        index=idx,
    )


def from_closes(closes: list[float], spread: float = 1.0, **kw: object) -> pd.DataFrame:
    opens = [closes[0], *closes[:-1]]
    highs = [max(o, c) + spread / 2 for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) - spread / 2 for o, c in zip(opens, closes, strict=True)]
    return frame(opens, highs, lows, closes, **kw)  # type: ignore[arg-type]


def zigzag(pivots: list[float], steps: int = 5, spread: float = 1.0) -> pd.DataFrame:
    """Linear path through `pivots` (`steps` bars per leg); highs/lows = close ± spread/2.

    Pivot k sits at bar index k * steps.
    """
    closes: list[float] = []
    for a, b in pairwise(pivots):
        closes += [a + (b - a) * i / steps for i in range(steps)]
    closes.append(pivots[-1])
    opens = [closes[0], *closes[:-1]]
    highs = [c + spread / 2 for c in closes]
    lows = [c - spread / 2 for c in closes]
    return frame(opens, highs, lows, closes)
