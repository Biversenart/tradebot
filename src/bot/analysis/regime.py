"""Market regime detection (spec §5.3.b): trend up/down, range, high-volatility/chaos.

Rules (thresholds from `analysis.regime` config):
1. ATR% >= `volatile_atr_ratio` x its trailing median (`volatility_lookback`) -> VOLATILE
2. ADX >= adx_trend and EMA-fast slope > 0 and close > EMA-slow -> TREND_UP
3. ADX >= adx_trend and EMA-fast slope < 0 and close < EMA-slow -> TREND_DOWN
4. otherwise -> RANGE
`squeeze` marks a Bollinger bandwidth in the bottom `squeeze_bb_percentile` (pre-breakout).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd

from bot.config.schema import IndicatorParams, RegimeParams
from bot.indicators import adx, atr_pct, bollinger, ema, rolling_percentile, slope


class Regime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"

    @property
    def label_tr(self) -> str:
        return {
            Regime.TREND_UP: "yükselen trend",
            Regime.TREND_DOWN: "düşen trend",
            Regime.RANGE: "yatay (range)",
            Regime.VOLATILE: "yüksek volatilite / kaos",
            Regime.UNKNOWN: "belirsiz (yetersiz veri)",
        }[self]


@dataclass(frozen=True)
class RegimeState:
    regime: Regime
    adx: float | None
    atr_pct: float | None
    atr_percentile: float | None
    bb_width_percentile: float | None
    ema_slope: float | None
    squeeze: bool


def regime_series(
    df: pd.DataFrame,
    params: RegimeParams | None = None,
    ind: IndicatorParams | None = None,
) -> pd.DataFrame:
    p = params or RegimeParams()
    ip = ind or IndicatorParams()
    close = df["close"]
    a = adx(df, ip.adx_period).adx
    ap = atr_pct(df, ip.atr_period)
    ap_pct = rolling_percentile(ap, p.percentile_lookback)
    ap_ratio = (
        ap / ap.rolling(p.volatility_lookback, min_periods=p.volatility_lookback // 4).median()
    )
    bb = bollinger(close, ip.bb_period, float(ip.bb_std))
    bw_pct = rolling_percentile(bb.bandwidth, p.percentile_lookback)
    fast = ema(close, p.ema_fast)
    slow = ema(close, p.ema_slow)
    sl = slope(fast, p.slope_bars)

    trend = a >= float(p.adx_trend)
    up = trend & (sl > 0) & (close > slow)
    down = trend & (sl < 0) & (close < slow)
    volatile = ap_ratio >= float(p.volatile_atr_ratio)

    regime = pd.Series(Regime.RANGE.value, index=df.index, dtype=object)
    regime[up] = Regime.TREND_UP.value
    regime[down] = Regime.TREND_DOWN.value
    regime[volatile] = Regime.VOLATILE.value
    regime[a.isna() | slow.isna() | sl.isna()] = Regime.UNKNOWN.value
    return pd.DataFrame(
        {
            "regime": regime,
            "adx": a,
            "atr_pct": ap,
            "atr_percentile": ap_pct,
            "atr_ratio": ap_ratio,
            "bb_width_percentile": bw_pct,
            "ema_slope": sl,
            "squeeze": bw_pct <= float(p.squeeze_bb_percentile),
        }
    )


def _f(v: object) -> float | None:
    if v is None:
        return None
    f = float(v)  # type: ignore[arg-type]
    return None if f != f else f  # NaN check


def detect_regime(
    df: pd.DataFrame,
    params: RegimeParams | None = None,
    ind: IndicatorParams | None = None,
) -> RegimeState:
    if df.empty:
        return RegimeState(Regime.UNKNOWN, None, None, None, None, None, False)
    last = regime_series(df, params, ind).iloc[-1]
    return RegimeState(
        regime=Regime(last["regime"]),
        adx=_f(last["adx"]),
        atr_pct=_f(last["atr_pct"]),
        atr_percentile=_f(last["atr_percentile"]),
        bb_width_percentile=_f(last["bb_width_percentile"]),
        ema_slope=_f(last["ema_slope"]),
        squeeze=bool(last["squeeze"]) if isinstance(last["squeeze"], bool | np.bool_) else False,
    )
