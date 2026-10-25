"""RSI mean reversion with Bollinger confirmation (spec §5.4.2). Range regime only.

Long: RSI crosses back above `buy_below` and the previous close was below the lower band.
Short mirrored with `sell_above` / upper band.
"""

from __future__ import annotations

import pandas as pd
from pydantic import Field

from bot.analysis.regime import Regime
from bot.indicators import bollinger, rsi
from bot.strategies.base import BaseStrategy, StrategyParams
from bot.strategies.common import (
    base_columns,
    clip_score,
    crossed_above,
    crossed_below,
    regime_mask,
    swing_stop,
)


class RsiReversionParams(StrategyParams):
    period: int = Field(default=14, gt=1)
    buy_below: float = Field(default=30, gt=0, lt=100)
    sell_above: float = Field(default=70, gt=0, lt=100)
    bb_period: int = Field(default=20, gt=1)
    bb_std: float = Field(default=2.0, gt=0)
    stop_lookback: int = Field(default=10, gt=0)


class RsiReversion(BaseStrategy):
    name = "rsi_reversion"
    Params = RsiReversionParams
    allowed_regimes = frozenset({Regime.RANGE})
    params: RsiReversionParams

    @property
    def warmup_bars(self) -> int:
        return self.analysis.regime.ema_slow + 50

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.empty_frame(df)
        a, regime = base_columns(df, self.analysis)
        r = rsi(df["close"], p.period)
        bb = bollinger(df["close"], p.bb_period, p.bb_std)
        ok = regime_mask(regime, self.allowed_regimes)
        touched_low = (df["low"] <= bb.lower).rolling(3, min_periods=1).max().astype(bool)
        touched_high = (df["high"] >= bb.upper).rolling(3, min_periods=1).max().astype(bool)
        long = crossed_above(r, p.buy_below) & touched_low & ok
        short = crossed_below(r, p.sell_above) & touched_high & ok
        buf = float(self.analysis.trade_plan.sl_atr_buffer)
        out.loc[long, "signal"] = 1
        out.loc[short, "signal"] = -1
        lo = swing_stop(df, a, p.stop_lookback, buf, True)
        hi = swing_stop(df, a, p.stop_lookback, buf, False)
        out["stop"] = lo.where(long, hi)
        depth = (r - 50).abs() / 50
        out["score"] = clip_score(50 + 50 * depth)
        out["atr"] = a
        out.loc[long, "reason"] = f"RSI {p.buy_below} üstüne döndü, alt Bollinger teyidi"
        out.loc[short, "reason"] = f"RSI {p.sell_above} altına döndü, üst Bollinger teyidi"
        return out
