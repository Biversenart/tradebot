from __future__ import annotations

from bot.analysis.structure import (
    Direction,
    SwingKind,
    analyze_structure,
    find_swings,
    swing_trend,
)
from tests.fixtures.loader import zigzag


def test_swings_and_labels_uptrend() -> None:
    df = zigzag([100, 110, 105, 115, 108, 120, 112], steps=5)
    swings = find_swings(df, 3)
    highs = [(s.index, s.price, s.label) for s in swings if s.kind is SwingKind.HIGH]
    lows = [(s.index, s.price, s.label) for s in swings if s.kind is SwingKind.LOW]
    assert highs == [(5, 110.5, None), (15, 115.5, "HH"), (25, 120.5, "HH")]
    assert lows == [(10, 104.5, None), (20, 107.5, "HL")]


def test_bos_in_uptrend_without_lookahead() -> None:
    df = zigzag([100, 110, 105, 115, 108, 120, 112], steps=5)
    ms = analyze_structure(df, 3)
    bos = [e for e in ms.events if e.kind == "BOS"]
    assert bos and all(e.direction is Direction.BULLISH for e in bos)
    assert [e.level for e in bos] == [110.5, 115.5]
    for e in ms.events:
        assert e.index >= e.swing_index + 3  # swing only known after confirmation
    assert ms.trend is Direction.BULLISH
    assert swing_trend(ms) is Direction.BULLISH


def test_choch_on_reversal() -> None:
    # higher highs/lows, then a break below the last higher low -> bearish CHoCH
    df = zigzag([100, 110, 105, 115, 108, 112, 95, 100], steps=5)
    ms = analyze_structure(df, 3)
    kinds = [(e.kind, e.direction) for e in ms.events]
    assert ("CHOCH", Direction.BEARISH) in kinds
    choch = next(e for e in ms.events if e.kind == "CHOCH")
    assert choch.level == 107.5
    assert ms.trend is Direction.BEARISH


def test_downtrend_labels() -> None:
    df = zigzag([120, 110, 115, 105, 112, 100, 108], steps=5)
    ms = analyze_structure(df, 3)
    assert swing_trend(ms) is Direction.BEARISH
    assert ms.trend is Direction.BEARISH
    assert [s.label for s in ms.lows] == [None, "LL", "LL"]
    assert [s.label for s in ms.highs] == [None, "LH"]
