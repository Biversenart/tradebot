from __future__ import annotations

import numpy as np
import pandas as pd

from bot.analysis.patterns import (
    candle_patterns,
    channel_breakout,
    chart_patterns,
    double_patterns,
    flags,
    head_shoulders,
    mark_at_zone,
    triangles,
)
from bot.analysis.structure import Direction, find_swings
from bot.analysis.zones import Zone, ZoneKind
from bot.config.schema import PatternParams
from bot.indicators import atr
from tests.fixtures.loader import frame, zigzag

P = PatternParams()


def names(df: pd.DataFrame) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for cp in candle_patterns(df):
        out.setdefault(cp.index, set()).add(cp.name)
    return out


def test_doji_hammer_shooting_star() -> None:
    df = frame([10, 10, 10], [10.5, 10.1, 13], [9.5, 7, 9.9], [10.02, 10.0 + 0.5 - 0.4, 10.3])
    n = names(df)
    assert "doji" in n[0]
    df2 = frame([10.0], [10.6], [8.0], [10.5])  # long lower wick, small upper wick
    assert "hammer" in names(df2)[0]
    df3 = frame([10.0], [12.0], [9.9], [9.6])
    assert "shooting_star" in names(df3)[0]


def test_engulfing() -> None:
    bull = frame([10, 9.4], [10.1, 10.6], [9.4, 9.3], [9.5, 10.5])
    assert "bullish_engulfing" in names(bull)[1]
    bear = frame([9.5, 10.6], [10.1, 10.7], [9.4, 9.3], [10, 9.4])
    assert "bearish_engulfing" in names(bear)[1]


def test_inside_bar() -> None:
    df = frame([10, 10.2], [11, 10.8], [9, 9.5], [10.5, 10.4])
    assert "inside_bar" in names(df)[1]


def test_morning_and_evening_star() -> None:
    ms = frame([12, 9.9, 10], [12.1, 10.0, 11.6], [9.9, 9.6, 9.9], [10, 9.8, 11.5])
    assert "morning_star" in names(ms)[2]
    es = frame([10, 12.1, 12], [12.1, 12.4, 12.1], [9.9, 12.0, 10.4], [12, 12.2, 10.5])
    assert "evening_star" in names(es)[2]


def test_mark_at_zone() -> None:
    df = frame([10.0, 20.0], [10.6, 20.6], [8.0, 18.0], [10.5, 20.5])
    pats = candle_patterns(df)
    zone = Zone(7.5, 8.5, ZoneKind.SUPPORT, 50, 3, 0, 0)
    marked = mark_at_zone(pats, df, [zone], pad=0.0)
    by_index = {p.index: p.at_zone for p in marked}
    assert by_index[0] is True
    assert by_index.get(1) in (None, False)


def test_double_top_confirmed() -> None:
    df = zigzag([100, 120, 108, 120.2, 104, 102], steps=6)
    sw = find_swings(df, 3)
    pats = double_patterns(df, sw, atr_value=2.0, p=P)
    dt = next(p for p in pats if p.name == "double_top")
    assert dt.direction is Direction.BEARISH and dt.confirmed
    assert dt.levels["neckline"] == 107.5


def test_double_bottom_unconfirmed() -> None:
    df = zigzag([110, 100, 108, 100.2, 105], steps=6)
    pats = double_patterns(df, find_swings(df, 3), atr_value=2.0, p=P)
    db = next(p for p in pats if p.name == "double_bottom")
    assert db.direction is Direction.BULLISH and not db.confirmed


def test_head_and_shoulders() -> None:
    df = zigzag([100, 115, 105, 125, 105.5, 115.3, 100, 98], steps=6)
    pats = head_shoulders(df, find_swings(df, 3), atr_value=2.0, p=P)
    hs = next(p for p in pats if p.name == "head_shoulders")
    assert hs.confirmed and hs.levels["head"] == 125.5


def test_ascending_triangle() -> None:
    df = zigzag([100, 120, 105, 120.1, 110, 119.9, 115, 118], steps=6)
    pats = triangles(df, find_swings(df, 3), atr_value=2.0, p=P)
    assert [p.name for p in pats] == ["ascending_triangle"]
    assert pats[0].direction is Direction.BULLISH and not pats[0].confirmed


def test_symmetrical_triangle_breakout_sets_direction() -> None:
    df = zigzag([100, 130, 104, 126, 108, 122, 112, 140], steps=6)
    pats = triangles(df, find_swings(df, 3), atr_value=2.0, p=P)
    assert pats and pats[0].name == "symmetrical_triangle"
    assert pats[0].confirmed and pats[0].direction is Direction.BULLISH


def test_bull_flag() -> None:
    closes = (
        [100.0] * 20
        + [100 + 3 * i for i in range(1, 11)]
        + [130, 129.5, 129, 129.5, 129, 128.8, 129.2, 129]
    )
    o = [closes[0], *closes[:-1]]
    df = frame(
        o,
        [max(a, b) + 0.5 for a, b in zip(o, closes, strict=True)],
        [min(a, b) - 0.5 for a, b in zip(o, closes, strict=True)],
        closes,
    )
    pats = flags(df, atr(df, 14), P)
    assert [p.name for p in pats] == ["bull_flag"]


def test_channel_breakout() -> None:
    rng = np.random.default_rng(0)
    closes = [*(100 + 0.1 * np.arange(60) + rng.normal(0, 0.2, 60)), 120.0]
    df = frame(closes, [c + 0.3 for c in closes], [c - 0.3 for c in closes], closes)
    pats = channel_breakout(df, P)
    assert pats and pats[0].name == "channel_breakout_up"
    calm = df.iloc[:-1]
    assert channel_breakout(calm, P) == []


def test_chart_patterns_runs_on_fixture() -> None:
    from tests.fixtures.loader import synthetic_1h

    df = synthetic_1h()
    pats = chart_patterns(df, find_swings(df, 3), atr(df, 14))
    assert all(p.start_index <= p.end_index for p in pats)
