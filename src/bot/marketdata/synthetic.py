"""Regime-switching synthetic OHLCV for offline demos and pipeline tests.

Written under exchange name "synthetic" so it can never be mistaken for real market data.
Segments (random lengths) alternate between trend up/down, range and high volatility.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from bot.core.timeframes import timeframe_delta

REGIMES = {
    "up": (0.0008, 0.006, False),
    "down": (-0.0008, 0.006, False),
    "range": (0.0, 0.005, True),
    "volatile": (0.0, 0.018, False),
}


def generate_ohlcv(
    start: datetime,
    end: datetime,
    timeframe: str = "1h",
    start_price: float = 100.0,
    seed: int = 0,
    min_segment: int = 150,
    max_segment: int = 1200,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(
        start, end, freq=timeframe_delta(timeframe), tz="UTC", inclusive="left", name="open_time"
    )
    n = len(idx)
    rows = np.empty((n, 5))
    price = start_price
    i = 0
    names = list(REGIMES)
    weights = np.array([0.3, 0.25, 0.35, 0.1])
    while i < n:
        regime = names[int(rng.choice(len(names), p=weights))]
        drift, vol, mean_revert = REGIMES[regime]
        seg = int(rng.integers(min_segment, max_segment))
        anchor = price
        for _ in range(min(seg, n - i)):
            o = price
            path = [o]
            for _ in range(4):
                r = drift / 4 + rng.normal(0, vol / 2)
                if mean_revert:
                    r += 0.04 * (np.log(anchor) - np.log(path[-1])) / 4
                path.append(path[-1] * float(np.exp(r)))
            c = path[-1]
            h = max(path) * (1 + abs(rng.normal(0, vol / 6)))
            lo = min(path) * (1 - abs(rng.normal(0, vol / 6)))
            v = float(rng.lognormal(5, 0.4) * (1 + 30 * abs(c / o - 1)))
            rows[i] = (o, h, lo, c, v)
            price = c
            i += 1
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)
    return df.round(8)
