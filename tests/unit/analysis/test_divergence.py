from __future__ import annotations

import numpy as np
import pandas as pd

from bot.analysis.divergence import DivergenceKind, find_divergences
from bot.analysis.structure import SwingKind, SwingPoint

T = pd.Timestamp("2024-01-01", tz="UTC")


def sp(i: int, price: float, kind: SwingKind) -> SwingPoint:
    return SwingPoint(i, T, price, kind)


def ind(values: dict[int, float], n: int = 100) -> pd.Series:
    s = pd.Series(np.full(n, 50.0))
    for k, v in values.items():
        s.iloc[k] = v
    return s


def test_regular_bullish() -> None:
    swings = [sp(10, 100, SwingKind.LOW), sp(30, 95, SwingKind.LOW)]
    d = find_divergences(swings, ind({10: 25, 30: 35}), "rsi")
    assert [x.kind for x in d] == [DivergenceKind.REGULAR_BULLISH]
    assert d[0].indicator == "rsi" and d[0].end_index == 30


def test_regular_bearish_and_hidden() -> None:
    swings = [
        sp(10, 100, SwingKind.HIGH),
        sp(30, 110, SwingKind.HIGH),  # HH, RSI LH -> regular bearish
        sp(20, 90, SwingKind.LOW),
        sp(40, 95, SwingKind.LOW),  # HL, RSI LL -> hidden bullish
    ]
    d = find_divergences(swings, ind({10: 75, 30: 65, 20: 40, 40: 30}), "rsi")
    assert {x.kind for x in d} == {DivergenceKind.REGULAR_BEARISH, DivergenceKind.HIDDEN_BULLISH}


def test_hidden_bearish() -> None:
    swings = [sp(10, 110, SwingKind.HIGH), sp(30, 105, SwingKind.HIGH)]
    d = find_divergences(swings, ind({10: 60, 30: 70}), "macd")
    assert d[0].kind is DivergenceKind.HIDDEN_BEARISH
    assert not d[0].kind.is_bullish


def test_distance_limits_and_nan() -> None:
    far = [sp(1, 100, SwingKind.LOW), sp(99, 95, SwingKind.LOW)]
    assert find_divergences(far, ind({1: 20, 99: 30}), "rsi") == []
    near = [sp(10, 100, SwingKind.LOW), sp(12, 95, SwingKind.LOW)]
    assert find_divergences(near, ind({10: 20, 12: 30}), "rsi") == []
    s = ind({10: 20})
    s.iloc[30] = np.nan
    assert find_divergences([sp(10, 100, SwingKind.LOW), sp(30, 95, SwingKind.LOW)], s, "x") == []


def test_no_divergence_when_confirming() -> None:
    swings = [sp(10, 100, SwingKind.LOW), sp(30, 95, SwingKind.LOW)]
    assert find_divergences(swings, ind({10: 35, 30: 25}), "rsi") == []
