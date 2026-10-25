"""Deterministic synthetic OHLCV fixture with known regime segments.

Regenerate: python tests/fixtures/generate_ohlcv.py
Segments (1h bars, starting 2024-01-01 UTC):
    0-399     strong uptrend      (drift +0.25%/bar, low vol)
    400-799   range               (mean-reverting around the level reached)
    800-1199  strong downtrend    (drift -0.25%/bar, low vol)
    1200-1499 high volatility     (no drift, 4x vol)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).with_name("synthetic_1h.csv")
SEGMENTS = [
    (400, 0.0025, 0.004, False),
    (400, 0.0, 0.004, True),
    (400, -0.0025, 0.004, False),
    (300, 0.0, 0.016, False),
]


def generate(seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    price = 100.0
    rows = []
    for n, drift, vol, mean_revert in SEGMENTS:
        anchor = price
        for _ in range(n):
            o = price
            path = [o]
            for _ in range(4):
                r = drift / 4 + rng.normal(0, vol / 2)
                if mean_revert:
                    r += 0.05 * (np.log(anchor) - np.log(path[-1])) / 4
                path.append(path[-1] * np.exp(r))
            c = path[-1]
            h = max(path) * (1 + abs(rng.normal(0, vol / 6)))
            lo = min(path) * (1 - abs(rng.normal(0, vol / 6)))
            v = float(rng.lognormal(3, 0.3) * (1 + 40 * abs(c / o - 1)))
            rows.append((o, h, lo, c, v))
            price = c
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="1h", tz="UTC", name="open_time")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)
    return df.round(6)


if __name__ == "__main__":
    generate().to_csv(OUT)
    print(f"yazıldı: {OUT}")
