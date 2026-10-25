from __future__ import annotations

import pandas as pd
import pytest

from bot.analysis.structure import SwingKind, SwingPoint, find_swings
from bot.analysis.zones import (
    PoolSide,
    ZoneKind,
    fibonacci,
    liquidity_pools,
    supply_demand,
    support_resistance,
)
from bot.indicators import atr
from tests.fixtures.loader import frame, zigzag

T = pd.Timestamp("2024-01-01", tz="UTC")


def sp(i: int, price: float, kind: SwingKind) -> SwingPoint:
    return SwingPoint(i, T, price, kind)


def test_support_and_resistance_clusters() -> None:
    df = zigzag([110, 100, 120, 100.3, 119.8, 99.8, 120.2, 110], steps=6)
    swings = find_swings(df, 3)
    zones = support_resistance(df, swings, atr_value=1.0)
    kinds = {z.kind for z in zones}
    assert kinds == {ZoneKind.SUPPORT, ZoneKind.RESISTANCE}
    sup = next(z for z in zones if z.kind is ZoneKind.SUPPORT)
    res = next(z for z in zones if z.kind is ZoneKind.RESISTANCE)
    assert sup.touches == 3 and res.touches == 3
    assert 99 <= sup.low <= sup.high <= 101
    assert 119 <= res.low <= res.high <= 121
    assert 0 < sup.strength <= 100
    assert sup.contains(99.5) and sup.distance(105) == pytest.approx(105 - sup.high)


def test_single_touch_not_a_zone() -> None:
    df = zigzag([100, 110, 90, 130], steps=6)
    assert support_resistance(df, find_swings(df, 3), atr_value=1.0) == []


def demand_frame(revisit: bool) -> pd.DataFrame:
    o = [100.0] * 15 + [100.0, 100.2] + [104.0, 105.0, 106.0]
    c = [100.0] * 15 + [100.2, 104.0] + [105.0, 106.0, 101.0 if revisit else 106.5]
    h = [max(a, b) + 0.5 for a, b in zip(o, c, strict=True)]
    lo = [min(a, b) - 0.5 for a, b in zip(o, c, strict=True)]
    return frame(o, h, lo, c)


def test_demand_zone_from_base_and_impulse() -> None:
    df = demand_frame(revisit=False)
    zones = supply_demand(df, atr(df, 14))
    d = [z for z in zones if z.kind is ZoneKind.DEMAND]
    assert len(d) == 1
    assert (d[0].low, d[0].high) == (99.5, 100.7)  # base candle range
    assert d[0].fresh and d[0].source == "impulse"


def test_demand_zone_not_fresh_after_revisit() -> None:
    df = demand_frame(revisit=True)
    d = [z for z in supply_demand(df, atr(df, 14)) if z.kind is ZoneKind.DEMAND]
    assert d and not d[0].fresh


def test_liquidity_pools_equal_highs_and_sweep() -> None:
    swings = [
        sp(5, 120.0, SwingKind.HIGH),
        sp(15, 120.05, SwingKind.HIGH),
        sp(10, 100.0, SwingKind.LOW),
    ]
    df = zigzag([110] * 5, steps=5)  # 21 bars, highs 110.5: not swept
    pools = liquidity_pools(df, swings, atr_value=1.0)
    assert len(pools) == 1
    p = pools[0]
    assert p.side is PoolSide.BUY_SIDE and p.count == 2 and p.price == 120.05
    assert not p.swept
    df2 = df.copy()
    highs = df2["high"].to_numpy(copy=True)
    highs[18] = 121.0
    df2["high"] = highs
    assert liquidity_pools(df2, swings, atr_value=1.0)[0].swept


def test_fibonacci_up_leg() -> None:
    fib = fibonacci([sp(5, 100.0, SwingKind.LOW), sp(10, 200.0, SwingKind.HIGH)])
    assert fib is not None and fib.direction == "up"
    assert fib.retracements[0.618] == pytest.approx(138.2)
    assert fib.retracements[0.5] == pytest.approx(150.0)
    assert fib.extensions[1.618] == pytest.approx(261.8)


def test_fibonacci_down_leg() -> None:
    fib = fibonacci([sp(5, 200.0, SwingKind.HIGH), sp(10, 100.0, SwingKind.LOW)])
    assert fib is not None and fib.direction == "down"
    assert fib.retracements[0.382] == pytest.approx(138.2)
    assert fib.extensions[1.272] == pytest.approx(200 - 127.2)
    assert fibonacci([]) is None
