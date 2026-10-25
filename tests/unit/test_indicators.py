"""Indicators checked against published reference values and closed-form results."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from bot.indicators import (
    adx,
    anchored_vwap,
    atr,
    bollinger,
    donchian,
    ema,
    macd,
    obv,
    rma,
    rsi,
    sma,
    stoch_rsi,
    true_range,
    vwap,
)
from tests.fixtures.loader import frame, from_closes, synthetic_1h

# StockCharts "RSI" ChartSchool worked example (14-period Wilder RSI).
STOCKCHARTS_CLOSES = [
    44.3389,
    44.0902,
    44.1497,
    43.6124,
    44.3278,
    44.8264,
    45.0955,
    45.4245,
    45.8433,
    46.0826,
    45.8931,
    46.0328,
    45.6140,
    46.2820,
    46.2820,
    46.0028,
    46.0328,
    46.4116,
    46.2222,
    45.6439,
    46.2122,
    46.2521,
    45.7137,
    46.4515,
    45.7835,
    45.3548,
    44.0288,
    44.1783,
    44.2181,
    44.5672,
    43.4205,
    42.6628,
    43.1314,
]
STOCKCHARTS_RSI = [
    70.53,
    66.32,
    66.55,
    69.41,
    66.36,
    57.97,
    62.93,
    63.26,
    56.06,
    62.38,
    54.71,
    50.42,
    39.99,
    41.46,
    41.87,
    45.46,
    37.30,
    33.08,
    37.77,
]
RAMP = pd.Series(np.arange(1.0, 61.0))


def test_rsi_matches_stockcharts_reference() -> None:
    r = rsi(pd.Series(STOCKCHARTS_CLOSES), 14)
    assert r.iloc[:14].isna().all()
    assert np.round(r.dropna().to_numpy(), 2).tolist() == STOCKCHARTS_RSI


def test_rsi_extremes() -> None:
    assert rsi(RAMP, 14).dropna().eq(100).all()
    assert rsi(RAMP[::-1].reset_index(drop=True), 14).dropna().eq(0).all()
    assert rsi(pd.Series([5.0] * 30), 14).dropna().eq(50).all()


def test_sma() -> None:
    assert sma(RAMP, 3).iloc[2] == 2.0
    assert sma(RAMP, 3).iloc[:2].isna().all()


def test_ema_seeded_with_sma_and_linear_lag() -> None:
    e = ema(RAMP, 3)
    assert e.iloc[2] == 2.0  # SMA(1,2,3)
    # for a ramp of slope 1, EMA(n) lags by (n-1)/2 exactly once seeded with the SMA
    assert np.allclose(e.iloc[2:], RAMP.iloc[2:] - 1.0)
    e10 = ema(RAMP, 10)
    assert np.allclose(e10.dropna(), RAMP.iloc[9:] - 4.5)


def test_rma_constant() -> None:
    assert np.allclose(rma(pd.Series([3.0] * 20), 5).dropna(), 3.0)


def test_ema_handles_leading_nan() -> None:
    s = pd.Series([np.nan, np.nan, 1, 2, 3, 4], dtype=float)
    e = ema(s, 3)
    assert e.iloc[4] == 2.0 and math.isnan(e.iloc[3])


def test_macd_on_ramp() -> None:
    m = macd(pd.Series(np.arange(1.0, 101.0)))
    # EMA lags (n-1)/2 on a unit ramp: MACD = 12.5 - 5.5 = 7
    assert np.allclose(m.macd.dropna(), 7.0)
    assert np.allclose(m.signal.dropna(), 7.0)
    assert np.allclose(m.hist.dropna(), 0.0)
    with pytest.raises(ValueError):
        macd(RAMP, fast=26, slow=12)


def test_bollinger_known_std() -> None:
    s = pd.Series(np.arange(1.0, 21.0))
    bb = bollinger(s, 20, 2.0)
    std = math.sqrt((20**2 - 1) / 12)
    assert bb.mid.iloc[-1] == 10.5
    assert bb.upper.iloc[-1] == pytest.approx(10.5 + 2 * std)
    assert bb.lower.iloc[-1] == pytest.approx(10.5 - 2 * std)
    assert bb.percent_b(s).iloc[-1] == pytest.approx((20 - (10.5 - 2 * std)) / (4 * std))
    flat = bollinger(pd.Series([7.0] * 25), 20)
    assert flat.bandwidth.dropna().eq(0).all()


def test_true_range_uses_previous_close_gap() -> None:
    df = frame([10, 15], [11, 16], [9, 14], [10, 15])
    tr = true_range(df)
    assert tr.tolist() == [2.0, 6.0]  # gap up: high 16 - prev close 10


def test_atr_constant_range() -> None:
    n = 30
    df = frame([10.0] * n, [11.0] * n, [9.0] * n, [10.0] * n)
    a = atr(df, 14)
    assert a.iloc[:13].isna().all()
    assert a.dropna().eq(2.0).all()


def test_adx_perfect_uptrend() -> None:
    n = 80
    lows = [float(i) for i in range(n)]
    df = frame([lo + 0.5 for lo in lows], [lo + 2 for lo in lows], lows, [lo + 1.5 for lo in lows])
    a = adx(df, 14)
    assert a.minus_di.dropna().eq(0).all()
    assert a.plus_di.dropna().iloc[-1] == pytest.approx(50.0)  # +DM 1 / TR 2
    assert a.adx.dropna().iloc[-1] == pytest.approx(100.0)


def test_obv() -> None:
    df = frame([1] * 5, [2] * 5, [0.5] * 5, [1, 2, 2, 1, 3], [10, 20, 30, 40, 50])
    assert obv(df).tolist() == [0, 20, 20, -20, 30]


def test_vwap_resets_daily() -> None:
    df = frame(
        [10, 10, 10],
        [12, 13, 11],
        [8, 9, 9],
        [10, 11, 10],
        [1, 3, 2],
        start="2024-01-01 22:00",
        freq="1h",
    )
    v = vwap(df)
    tp = [(12 + 8 + 10) / 3, (13 + 9 + 11) / 3, (11 + 9 + 10) / 3]
    assert v.iloc[0] == pytest.approx(tp[0])
    assert v.iloc[1] == pytest.approx((tp[0] * 1 + tp[1] * 3) / 4)
    assert v.iloc[2] == pytest.approx(tp[2])  # new UTC day -> reset


def test_anchored_vwap() -> None:
    df = from_closes([10, 11, 12, 13], spread=0)
    av = anchored_vwap(df, 2)
    assert math.isnan(av.iloc[1])
    assert av.iloc[2] == pytest.approx(
        df["close"].iloc[2] * 0 + (12 + 12 + 11) / 3 * 1 / 1, rel=0.05
    )
    with pytest.raises(IndexError):
        anchored_vwap(df, 10)


def test_donchian() -> None:
    df = frame([1] * 5, [2, 6, 4, 3, 5], [1, 4, 2, 1.5, 3], [1] * 5)
    d = donchian(df, 3)
    assert d.upper.iloc[2] == 6 and d.lower.iloc[2] == 1
    assert d.upper.iloc[4] == 5 and d.lower.iloc[4] == 1.5
    assert d.mid.iloc[4] == 3.25


def test_stoch_rsi_bounds_and_formula() -> None:
    close = synthetic_1h()["close"]
    s = stoch_rsi(close)
    k = s.k.dropna()
    assert ((k >= 0) & (k <= 100)).all()
    r = rsi(close, 14)
    raw = (r - r.rolling(14).min()) / (r.rolling(14).max() - r.rolling(14).min()) * 100
    assert np.allclose(s.k.dropna(), raw.rolling(3).mean().dropna().loc[s.k.dropna().index])


def test_invalid_period() -> None:
    with pytest.raises(ValueError):
        sma(RAMP, 0)
