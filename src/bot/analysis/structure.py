"""Market structure (spec §5.3.c): fractal swing points, HH/HL/LH/LL labels, BOS and CHoCH.

- A swing high at bar i: high[i] > highs of the `n` bars before and >= highs of the `n` bars
  after (fractal). It is only *confirmed* at bar i+n (no look-ahead in event detection).
- BOS (break of structure): close beyond the last confirmed swing in the trend direction
  (or the first break from a neutral state).
- CHoCH (change of character): close beyond the last confirmed swing AGAINST the current
  structural trend -> trend flips.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

import pandas as pd


class SwingKind(StrEnum):
    HIGH = "high"
    LOW = "low"


class Direction(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"

    @property
    def label_tr(self) -> str:
        return {"bullish": "yükseliş", "bearish": "düşüş", "neutral": "nötr"}[self.value]


@dataclass(frozen=True)
class SwingPoint:
    index: int
    time: pd.Timestamp
    price: float
    kind: SwingKind
    label: str | None = None  # HH, LH (highs) / HL, LL (lows)


@dataclass(frozen=True)
class StructureEvent:
    index: int
    time: pd.Timestamp
    kind: str  # "BOS" | "CHOCH"
    direction: Direction
    level: float
    swing_index: int


@dataclass(frozen=True)
class MarketStructure:
    swings: list[SwingPoint]
    events: list[StructureEvent]
    trend: Direction
    lookback: int

    @property
    def highs(self) -> list[SwingPoint]:
        return [s for s in self.swings if s.kind is SwingKind.HIGH]

    @property
    def lows(self) -> list[SwingPoint]:
        return [s for s in self.swings if s.kind is SwingKind.LOW]

    @property
    def last_event(self) -> StructureEvent | None:
        return self.events[-1] if self.events else None


def find_swings(df: pd.DataFrame, n: int = 3) -> list[SwingPoint]:
    if n < 1:
        raise ValueError("swing_lookback >= 1 olmalı")
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    idx = pd.DatetimeIndex(df.index)
    out: list[SwingPoint] = []
    for i in range(n, len(df) - n):
        h, lo = highs[i], lows[i]
        if h > highs[i - n : i].max() and h >= highs[i + 1 : i + n + 1].max():
            out.append(SwingPoint(i, idx[i], float(h), SwingKind.HIGH))
        if lo < lows[i - n : i].min() and lo <= lows[i + 1 : i + n + 1].min():
            out.append(SwingPoint(i, idx[i], float(lo), SwingKind.LOW))
    return label_swings(out)


def label_swings(swings: list[SwingPoint]) -> list[SwingPoint]:
    prev: dict[SwingKind, SwingPoint] = {}
    out: list[SwingPoint] = []
    for s in swings:
        p = prev.get(s.kind)
        label = None
        if p is not None:
            if s.kind is SwingKind.HIGH:
                label = "HH" if s.price > p.price else "LH"
            else:
                label = "HL" if s.price > p.price else "LL"
        s = replace(s, label=label)
        out.append(s)
        prev[s.kind] = s
    return out


def analyze_structure(df: pd.DataFrame, n: int = 3) -> MarketStructure:
    swings = find_swings(df, n)
    closes = df["close"].to_numpy(dtype=float)
    idx = pd.DatetimeIndex(df.index)
    by_confirm: dict[int, list[SwingPoint]] = {}
    for s in swings:
        by_confirm.setdefault(s.index + n, []).append(s)

    trend = Direction.NEUTRAL
    last_high: SwingPoint | None = None
    last_low: SwingPoint | None = None
    broken: set[tuple[int, SwingKind]] = set()
    events: list[StructureEvent] = []
    for t in range(len(df)):
        for s in by_confirm.get(t, []):
            if s.kind is SwingKind.HIGH:
                last_high = s
            else:
                last_low = s
        c = closes[t]
        if (
            last_high is not None
            and (last_high.index, SwingKind.HIGH) not in broken
            and c > last_high.price
        ):
            kind = "CHOCH" if trend is Direction.BEARISH else "BOS"
            events.append(
                StructureEvent(t, idx[t], kind, Direction.BULLISH, last_high.price, last_high.index)
            )
            broken.add((last_high.index, SwingKind.HIGH))
            trend = Direction.BULLISH
        elif (
            last_low is not None
            and (last_low.index, SwingKind.LOW) not in broken
            and c < last_low.price
        ):
            kind = "CHOCH" if trend is Direction.BULLISH else "BOS"
            events.append(
                StructureEvent(t, idx[t], kind, Direction.BEARISH, last_low.price, last_low.index)
            )
            broken.add((last_low.index, SwingKind.LOW))
            trend = Direction.BEARISH
    return MarketStructure(swings, events, trend, n)


def swing_trend(structure: MarketStructure) -> Direction:
    """Trend by labels: last high HH and last low HL -> bullish; LH + LL -> bearish."""
    highs, lows = structure.highs, structure.lows
    if not highs or not lows:
        return Direction.NEUTRAL
    h, lo = highs[-1].label, lows[-1].label
    if h == "HH" and lo == "HL":
        return Direction.BULLISH
    if h == "LH" and lo == "LL":
        return Direction.BEARISH
    return Direction.NEUTRAL
