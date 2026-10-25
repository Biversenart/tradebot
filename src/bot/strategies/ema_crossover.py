"""EMA crossover with higher-trend filter (spec §5.4.1).

Long: EMA fast crosses above EMA slow while close > EMA trend_filter; short mirrored.
Trades only in trending regimes. Stop: swing low/high of `stop_lookback` bars ± ATR buffer.
"""

from __future__ import annotations

import pandas as pd
from pydantic import Field

from bot.analysis.regime import Regime
from bot.indicators import ema
from bot.strategies.base import BaseStrategy, StrategyParams
from bot.strategies.common import (
    base_columns,
    clip_score,
    crossed_above,
    crossed_below,
    regime_mask,
    swing_stop,
)


class EmaCrossoverParams(StrategyParams):
    fast: int = Field(default=9, gt=0)
    slow: int = Field(default=21, gt=0)
    trend_filter: int = Field(default=200, gt=0)
    stop_lookback: int = Field(default=10, gt=0)


class EmaCrossover(BaseStrategy):
    name = "ema_crossover"
    Params = EmaCrossoverParams
    allowed_regimes = frozenset({Regime.TREND_UP, Regime.TREND_DOWN})
    params: EmaCrossoverParams

    @property
    def warmup_bars(self) -> int:
        return max(self.params.trend_filter, self.analysis.regime.ema_slow) + 50

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.empty_frame(df)
        a, regime = base_columns(df, self.analysis)
        fast, slow, trend = (
            ema(df["close"], p.fast),
            ema(df["close"], p.slow),
            ema(df["close"], p.trend_filter),
        )
        ok = regime_mask(regime, self.allowed_regimes)
        buf = float(self.analysis.trade_plan.sl_atr_buffer)
        long = crossed_above(fast, slow) & (df["close"] > trend) & ok
        short = crossed_below(fast, slow) & (df["close"] < trend) & ok
        sep = ((fast - slow).abs() / a).fillna(0)
        score = clip_score(
            50 + 25 * sep + 25 * ((df["close"] - trend).abs() / (3 * a)).clip(upper=1)
        )
        out.loc[long, "signal"] = 1
        out.loc[short, "signal"] = -1
        out["stop"] = swing_stop(df, a, p.stop_lookback, buf, True).where(
            long, swing_stop(df, a, p.stop_lookback, buf, False)
        )
        out["score"] = score
        out["atr"] = a
        out.loc[long, "reason"] = (
            f"EMA{p.fast} EMA{p.slow}'i yukarı kesti, fiyat EMA{p.trend_filter} üstünde"
        )
        out.loc[short, "reason"] = (
            f"EMA{p.fast} EMA{p.slow}'i aşağı kesti, fiyat EMA{p.trend_filter} altında"
        )
        return out
