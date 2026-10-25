"""Technical indicators on pandas Series/DataFrames (float maths; not used for money).

Conventions:
- Input DataFrame columns: open, high, low, close, volume (DatetimeIndex, UTC).
- Moving averages are seeded with the SMA of the first `n` values (TradingView/TA-Lib style);
  values before enough data are NaN.
- Wilder smoothing (RMA): alpha = 1/n, seeded with SMA.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def _check_period(n: int) -> None:
    if n <= 0:
        raise ValueError("Periyot pozitif olmalı.")


def sma(s: pd.Series, n: int) -> pd.Series:
    _check_period(n)
    return s.rolling(n, min_periods=n).mean()


def _seeded_ewm(s: pd.Series, n: int, alpha: float) -> pd.Series:
    values = s.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < n:
        return pd.Series(out, index=s.index)
    start = valid[0]
    seed_end = start + n - 1
    window = values[start : seed_end + 1]
    if np.isnan(window).any():
        return pd.Series(out, index=s.index)
    out[seed_end] = window.mean()
    for i in range(seed_end + 1, len(values)):
        v = values[i]
        out[i] = out[i - 1] if np.isnan(v) else alpha * v + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=s.index)


def ema(s: pd.Series, n: int) -> pd.Series:
    _check_period(n)
    return _seeded_ewm(s, n, 2.0 / (n + 1))


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average."""
    _check_period(n)
    return _seeded_ewm(s, n, 1.0 / n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = rma(gain.iloc[1:], n).reindex(close.index)
    avg_loss = rma(loss.iloc[1:], n).reindex(close.index)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100 - 100 / (1 + rs)
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out.where(avg_gain.notna())


@dataclass(frozen=True)
class Macd:
    macd: pd.Series
    signal: pd.Series
    hist: pd.Series


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Macd:
    if fast >= slow:
        raise ValueError("MACD: fast < slow olmalı.")
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return Macd(line, sig, line - sig)


@dataclass(frozen=True)
class StochRsi:
    k: pd.Series
    d: pd.Series


def stoch_rsi(
    close: pd.Series, rsi_period: int = 14, stoch_period: int = 14, k: int = 3, d: int = 3
) -> StochRsi:
    r = rsi(close, rsi_period)
    lo = r.rolling(stoch_period, min_periods=stoch_period).min()
    hi = r.rolling(stoch_period, min_periods=stoch_period).max()
    rng = hi - lo
    raw = (
        ((r - lo) / rng.replace(0, np.nan) * 100)
        .where(rng != 0, 50.0)
        .where(r.notna() & lo.notna())
    )
    k_line = sma(raw, k)
    return StochRsi(k_line, sma(k_line, d))


@dataclass(frozen=True)
class Bollinger:
    mid: pd.Series
    upper: pd.Series
    lower: pd.Series

    @property
    def bandwidth(self) -> pd.Series:
        """(upper - lower) / mid."""
        return (self.upper - self.lower) / self.mid

    def percent_b(self, close: pd.Series) -> pd.Series:
        return (close - self.lower) / (self.upper - self.lower)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> Bollinger:
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std(ddof=0)
    return Bollinger(mid, mid + k * std, mid - k * std)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    )
    tr = ranges.max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return rma(true_range(df), n)


def atr_pct(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return atr(df, n) / df["close"] * 100


@dataclass(frozen=True)
class Adx:
    adx: pd.Series
    plus_di: pd.Series
    minus_di: pd.Series


def adx(df: pd.DataFrame, n: int = 14) -> Adx:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = true_range(df)
    atr_ = rma(tr.iloc[1:], n)
    plus_di = 100 * rma(plus_dm.iloc[1:], n) / atr_
    minus_di = 100 * rma(minus_dm.iloc[1:], n) / atr_
    di_sum = plus_di + minus_di
    dx = (
        (100 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan))
        .fillna(0.0)
        .where(plus_di.notna())
    )
    adx_ = rma(dx, n)
    idx = df.index
    return Adx(adx_.reindex(idx), plus_di.reindex(idx), minus_di.reindex(idx))


def obv(df: pd.DataFrame) -> pd.Series:
    diff = df["close"].diff()
    direction = pd.Series(np.sign(diff.to_numpy()), index=df.index).fillna(0.0)
    return (direction * df["volume"]).cumsum()


def typical_price(df: pd.DataFrame) -> pd.Series:
    return (df["high"] + df["low"] + df["close"]) / 3


def vwap(df: pd.DataFrame, session: str = "D") -> pd.Series:
    """Session VWAP (resets every `session`, default daily UTC)."""
    tp = typical_price(df)
    idx = pd.DatetimeIndex(df.index)
    key = idx.floor(session)
    pv = (tp * df["volume"]).groupby(key).cumsum()
    vol = df["volume"].groupby(key).cumsum()
    return (pv / vol.replace(0, np.nan)).ffill()


def anchored_vwap(df: pd.DataFrame, anchor: int) -> pd.Series:
    """VWAP anchored at positional index `anchor` (NaN before it)."""
    if not 0 <= anchor < len(df):
        raise IndexError("anchor aralık dışında")
    tp = typical_price(df).iloc[anchor:]
    vol = df["volume"].iloc[anchor:]
    out = (tp * vol).cumsum() / vol.cumsum().replace(0, np.nan)
    return out.reindex(df.index)


@dataclass(frozen=True)
class Donchian:
    upper: pd.Series
    lower: pd.Series

    @property
    def mid(self) -> pd.Series:
        return (self.upper + self.lower) / 2


def donchian(df: pd.DataFrame, n: int = 20) -> Donchian:
    return Donchian(
        df["high"].rolling(n, min_periods=n).max(), df["low"].rolling(n, min_periods=n).min()
    )


def slope(s: pd.Series, n: int = 5) -> pd.Series:
    """Relative slope: (s_t - s_{t-n}) / s_{t-n} per bar."""
    _check_period(n)
    return (s - s.shift(n)) / s.shift(n) / n


def rolling_percentile(s: pd.Series, n: int) -> pd.Series:
    """Percentile rank (0..1) of each value within the trailing `n` window."""
    _check_period(n)
    return s.rolling(n, min_periods=max(2, n // 4)).rank(pct=True)
