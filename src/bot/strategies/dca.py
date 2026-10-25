"""DCA (spec §5.4.5): scheduled or dip buys. Long only, wide protective stop.

Buys every `interval_bars`, or (if `dip_pct` > 0) only when price is `dip_pct` % below its
rolling `dip_lookback` high. Never in a strong downtrend unless `allow_in_downtrend`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from bot.analysis.regime import Regime
from bot.core.timeframes import timeframe_delta
from bot.strategies.base import BaseStrategy, StrategyParams
from bot.strategies.common import base_columns


class DcaParams(StrategyParams):
    interval_bars: int = Field(default=24, gt=0)
    dip_pct: float = Field(default=0, ge=0, lt=100)
    dip_lookback: int = Field(default=72, gt=0)
    stop_pct: float = Field(default=8.0, gt=0, lt=100)
    allow_in_downtrend: bool = False


class Dca(BaseStrategy):
    name = "dca"
    Params = DcaParams
    allow_short = False
    max_positions = 5
    params: DcaParams

    @property
    def warmup_bars(self) -> int:
        return self.analysis.regime.ema_slow + 50

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.empty_frame(df)
        a, regime = base_columns(df, self.analysis)
        # schedule anchored on absolute bar time -> identical live and in backtests
        step = int(timeframe_delta(self.timeframe).total_seconds())
        secs = np.array([int(t.timestamp()) for t in pd.DatetimeIndex(df.index)], dtype=np.int64)
        cond = pd.Series((secs // step) % p.interval_bars == 0, index=df.index)
        if p.dip_pct > 0:
            peak = df["high"].rolling(p.dip_lookback, min_periods=1).max()
            cond = cond & (df["close"] <= peak * (1 - p.dip_pct / 100))
        if not p.allow_in_downtrend:
            cond = cond & (regime != Regime.TREND_DOWN.value)
        cond = cond & (regime != Regime.UNKNOWN.value)
        out.loc[cond, "signal"] = 1
        out["stop"] = df["close"] * (1 - p.stop_pct / 100)
        out["score"] = 60.0
        out["atr"] = a
        out.loc[cond, "reason"] = "zamanlanmış kademeli alım" + (" (düşüşte)" if p.dip_pct else "")
        return out
