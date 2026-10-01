"""Donchian breakout with volume confirmation and ATR stop (spec §5.4.3).

Long: close above the previous `donchian`-bar high with volume >= volume_mult x average.
Preferred after a squeeze (Bollinger bandwidth in its low percentile).
"""

from __future__ import annotations

import pandas as pd
from pydantic import Field

from bot.analysis.regime import Regime, regime_series
from bot.indicators import atr, donchian, sma
from bot.strategies.base import BaseStrategy, StrategyParams
from bot.strategies.common import clip_score


class BreakoutParams(StrategyParams):
    donchian: int = Field(default=20, gt=1)
    volume_mult: float = Field(default=1.5, gt=0)
    volume_period: int = Field(default=20, gt=1)
    atr_stop_mult: float = Field(default=2.0, gt=0)
    require_squeeze: bool = False
    squeeze_lookback: int = Field(default=20, gt=0)


class Breakout(BaseStrategy):
    name = "breakout"
    Params = BreakoutParams
    allowed_regimes = frozenset({Regime.RANGE, Regime.TREND_UP, Regime.TREND_DOWN})
    params: BreakoutParams

    @property
    def warmup_bars(self) -> int:
        return self.analysis.regime.ema_slow + 50

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.empty_frame(df)
        a = atr(df, self.analysis.indicators.atr_period)
        rs = regime_series(df, self.analysis.regime, self.analysis.indicators)
        ok = rs["regime"].isin([r.value for r in self.allowed_regimes])
        d = donchian(df, p.donchian)
        upper, lower = d.upper.shift(1), d.lower.shift(1)
        vol_ok = df["volume"] >= p.volume_mult * sma(df["volume"], p.volume_period).shift(1)
        if p.require_squeeze:
            sq = (
                rs["squeeze"]
                .astype(bool)
                .rolling(p.squeeze_lookback, min_periods=1)
                .max()
                .astype(bool)
            )
            ok = ok & sq
        long = (df["close"] > upper) & vol_ok & ok
        short = (df["close"] < lower) & vol_ok & ok
        out.loc[long, "signal"] = 1
        out.loc[short, "signal"] = -1
        out["stop"] = (df["close"] - p.atr_stop_mult * a).where(
            long, df["close"] + p.atr_stop_mult * a
        )
        strength = ((df["close"] - upper).where(long, lower - df["close"]) / a).clip(lower=0)
        out["score"] = clip_score(55 + 30 * strength.clip(upper=1) + 15 * (vol_ok.astype(float)))
        out["atr"] = a
        out.loc[long, "reason"] = f"{p.donchian} mumluk Donchian üst bandı hacimle kırıldı"
        out.loc[short, "reason"] = f"{p.donchian} mumluk Donchian alt bandı hacimle kırıldı"
        return out
