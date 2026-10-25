"""Shared causal helpers for strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd

from bot.analysis.regime import Regime, regime_series
from bot.config.schema import AnalysisConfig
from bot.indicators import atr


def base_columns(df: pd.DataFrame, analysis: AnalysisConfig) -> tuple[pd.Series, pd.Series]:
    """(atr, regime) series - both causal."""
    a = atr(df, analysis.indicators.atr_period)
    r = regime_series(df, analysis.regime, analysis.indicators)["regime"]
    return a, r


def regime_mask(regime: pd.Series, allowed: frozenset[Regime]) -> pd.Series:
    return regime.isin([r.value for r in allowed])


def swing_stop(
    df: pd.DataFrame, a: pd.Series, lookback: int, buffer_atr: float, long: bool
) -> pd.Series:
    """Structural stop: lowest low (highest high) of the last `lookback` bars ± buffer x ATR."""
    if long:
        return df["low"].rolling(lookback, min_periods=1).min() - buffer_atr * a
    return df["high"].rolling(lookback, min_periods=1).max() + buffer_atr * a


def crossed_above(a: pd.Series, b: pd.Series | float) -> pd.Series:
    prev_a = a.shift(1)
    prev_b = b.shift(1) if isinstance(b, pd.Series) else b
    return (a > b) & (prev_a <= prev_b)


def crossed_below(a: pd.Series, b: pd.Series | float) -> pd.Series:
    prev_a = a.shift(1)
    prev_b = b.shift(1) if isinstance(b, pd.Series) else b
    return (a < b) & (prev_a >= prev_b)


def clip_score(s: pd.Series) -> pd.Series:
    return s.clip(lower=0, upper=100).fillna(0.0)


def nan_series(index: pd.Index) -> pd.Series:
    return pd.Series(np.nan, index=index)
