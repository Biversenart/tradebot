"""Grid (spec §5.4.4): staggered buys inside a price range, range regime only.

If `lower`/`upper` are 0 the range is the Donchian channel of `range_bars` (auto).
Buy when price crosses down through a grid level; target = next level up (TP1 at 1 grid step
is emulated via the R multiples: stop distance = `stop_levels` grid steps below the range).
Long only (spot friendly).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from bot.analysis.regime import Regime
from bot.indicators import donchian
from bot.strategies.base import BaseStrategy, StrategyParams
from bot.strategies.common import base_columns, regime_mask


class GridParams(StrategyParams):
    lower: float = Field(default=0, ge=0)
    upper: float = Field(default=0, ge=0)
    levels: int = Field(default=10, ge=2)
    range_bars: int = Field(default=100, ge=10)
    stop_buffer_atr: float = Field(default=1.0, gt=0)


class Grid(BaseStrategy):
    name = "grid"
    Params = GridParams
    allowed_regimes = frozenset({Regime.RANGE})
    allow_short = False
    max_positions = 5
    params: GridParams

    @property
    def warmup_bars(self) -> int:
        return max(self.params.range_bars, self.analysis.regime.ema_slow) + 50

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.empty_frame(df)
        a, regime = base_columns(df, self.analysis)
        ok = regime_mask(regime, self.allowed_regimes)
        if p.lower > 0 and p.upper > p.lower:
            lo = pd.Series(p.lower, index=df.index)
            hi = pd.Series(p.upper, index=df.index)
        else:
            d = donchian(df, p.range_bars)
            lo, hi = d.lower.shift(1), d.upper.shift(1)
        step = (hi - lo) / p.levels
        close, prev = df["close"], df["close"].shift(1)
        # level index (floor) of current and previous close inside the range
        cur_lvl = np.floor((close - lo) / step)
        prev_lvl = np.floor((prev - lo) / step)
        inside = (close > lo) & (close < hi)
        long = (cur_lvl < prev_lvl) & inside & ok & (cur_lvl < p.levels / 2)
        out.loc[long, "signal"] = 1
        out["stop"] = lo - p.stop_buffer_atr * a
        out["score"] = (60 + 40 * (1 - cur_lvl / p.levels)).clip(0, 100).fillna(0)
        out["atr"] = a
        out.loc[long, "reason"] = "fiyat grid seviyesinin altına indi (alt yarı)"
        return out
