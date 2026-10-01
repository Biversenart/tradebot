"""Price zones (spec §5.3.d): support/resistance, supply/demand, liquidity pools, Fibonacci."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd

from bot.analysis.structure import SwingKind, SwingPoint
from bot.config.schema import ZoneParams


class ZoneKind(StrEnum):
    SUPPORT = "support"
    RESISTANCE = "resistance"
    DEMAND = "demand"
    SUPPLY = "supply"

    @property
    def label_tr(self) -> str:
        return {
            "support": "destek",
            "resistance": "direnç",
            "demand": "talep bölgesi",
            "supply": "arz bölgesi",
        }[self.value]

    @property
    def is_bullish(self) -> bool:
        return self in (ZoneKind.SUPPORT, ZoneKind.DEMAND)


@dataclass(frozen=True)
class Zone:
    low: float
    high: float
    kind: ZoneKind
    strength: float  # 0..100
    touches: int
    first_index: int
    last_index: int
    fresh: bool = True
    source: str = "swing_cluster"

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    def contains(self, price: float, pad: float = 0.0) -> bool:
        return self.low - pad <= price <= self.high + pad

    def distance(self, price: float) -> float:
        """0 inside the zone, otherwise distance to the nearest edge."""
        if self.contains(price):
            return 0.0
        return self.low - price if price < self.low else price - self.high


def _classify(low: float, high: float, price: float) -> ZoneKind:
    return ZoneKind.SUPPORT if (low + high) / 2 <= price else ZoneKind.RESISTANCE


def support_resistance(
    df: pd.DataFrame, swings: list[SwingPoint], atr_value: float, params: ZoneParams | None = None
) -> list[Zone]:
    """Cluster swing prices within `cluster_atr` x ATR; strength = touches, recency, volume."""
    p = params or ZoneParams()
    if not swings or atr_value <= 0:
        return []
    tol = float(p.cluster_atr) * atr_value
    vols = df["volume"].to_numpy(dtype=float)
    price = float(df["close"].iloc[-1])
    clusters: list[list[SwingPoint]] = []
    for s in sorted(swings, key=lambda s: s.price):
        if clusters and s.price - np.mean([c.price for c in clusters[-1]]) <= tol:
            clusters[-1].append(s)
        else:
            clusters.append([s])
    n = len(df)
    raw: list[tuple[float, float, int, int, int, float]] = []
    for cl in clusters:
        if len(cl) < p.min_touches:
            continue
        prices = [c.price for c in cl]
        lo, hi = min(prices), max(prices)
        if hi - lo < 0.2 * atr_value:  # give degenerate zones a minimum height
            pad = (0.2 * atr_value - (hi - lo)) / 2
            lo, hi = lo - pad, hi + pad
        first, last = min(c.index for c in cl), max(c.index for c in cl)
        volume = float(sum(vols[c.index] for c in cl))
        raw.append((lo, hi, len(cl), first, last, volume))
    if not raw:
        return []
    max_touch = max(r[2] for r in raw)
    max_vol = max(r[5] for r in raw) or 1.0
    zones = []
    for lo, hi, touches, first, last, volume in raw:
        strength = 100 * (0.4 * touches / max_touch + 0.3 * (last + 1) / n + 0.3 * volume / max_vol)
        zones.append(
            Zone(lo, hi, _classify(lo, hi, price), round(strength, 2), touches, first, last)
        )
    zones.sort(key=lambda z: z.strength, reverse=True)
    return zones[: p.max_zones]


def supply_demand(df: pd.DataFrame, atr: pd.Series, params: ZoneParams | None = None) -> list[Zone]:
    """Base candle(s) followed by an impulse candle (body >= impulse_atr x ATR)."""
    p = params or ZoneParams()
    o = df["open"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    a = atr.to_numpy(dtype=float)
    zones: list[Zone] = []
    for i in range(1, len(df)):
        if np.isnan(a[i]) or a[i] <= 0:
            continue
        body = abs(c[i] - o[i])
        base_body = abs(c[i - 1] - o[i - 1])
        if body < float(p.impulse_atr) * a[i] or base_body > float(p.base_max_body_atr) * a[i]:
            continue
        zlo, zhi = float(lo[i - 1]), float(h[i - 1])
        bullish = c[i] > o[i]
        later_low = lo[i + 1 :].min() if i + 1 < len(df) else np.inf
        later_high = h[i + 1 :].max() if i + 1 < len(df) else -np.inf
        if bullish:
            fresh = bool(later_low > zhi)
            kind = ZoneKind.DEMAND
        else:
            fresh = bool(later_high < zlo)
            kind = ZoneKind.SUPPLY
        strength = min(100.0, 100 * body / (3 * a[i])) * (1.0 if fresh else 0.5)
        zones.append(Zone(zlo, zhi, kind, round(strength, 2), 1, i - 1, i, fresh, "impulse"))
    zones.sort(key=lambda z: (z.fresh, z.last_index), reverse=True)
    return zones[: p.max_zones]


class PoolSide(StrEnum):
    BUY_SIDE = "buy_side"  # resting buy stops above equal highs
    SELL_SIDE = "sell_side"  # resting sell stops below equal lows


@dataclass(frozen=True)
class LiquidityPool:
    price: float
    side: PoolSide
    count: int
    indices: tuple[int, ...]
    swept: bool = False


def liquidity_pools(
    df: pd.DataFrame, swings: list[SwingPoint], atr_value: float, params: ZoneParams | None = None
) -> list[LiquidityPool]:
    """Equal highs / equal lows (within `equal_level_atr` x ATR)."""
    p = params or ZoneParams()
    tol = float(p.equal_level_atr) * atr_value
    pools: list[LiquidityPool] = []
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    for kind, side in ((SwingKind.HIGH, PoolSide.BUY_SIDE), (SwingKind.LOW, PoolSide.SELL_SIDE)):
        pts = sorted((s for s in swings if s.kind is kind), key=lambda s: s.price)
        group: list[SwingPoint] = []
        groups: list[list[SwingPoint]] = []
        for s in pts:
            if group and s.price - group[0].price > tol:
                groups.append(group)
                group = []
            group.append(s)
        if group:
            groups.append(group)
        for g in groups:
            if len(g) < 2:
                continue
            last = max(x.index for x in g)
            if side is PoolSide.BUY_SIDE:
                level = max(x.price for x in g)
                swept = bool(last + 1 < len(df) and highs[last + 1 :].max() > level)
            else:
                level = min(x.price for x in g)
                swept = bool(last + 1 < len(df) and lows[last + 1 :].min() < level)
            pools.append(
                LiquidityPool(level, side, len(g), tuple(sorted(x.index for x in g)), swept)
            )
    return pools


RETRACEMENTS = (0.236, 0.382, 0.5, 0.618, 0.786)
EXTENSIONS = (1.272, 1.618)


@dataclass(frozen=True)
class FibLevels:
    start: float  # leg start price
    end: float  # leg end price
    direction: str  # "up" | "down"
    retracements: dict[float, float] = field(default_factory=dict)
    extensions: dict[float, float] = field(default_factory=dict)


def fibonacci(swings: list[SwingPoint]) -> FibLevels | None:
    """Levels of the most recent swing leg (last high <-> last low)."""
    highs = [s for s in swings if s.kind is SwingKind.HIGH]
    lows = [s for s in swings if s.kind is SwingKind.LOW]
    if not highs or not lows:
        return None
    h, lo = highs[-1], lows[-1]
    rng = h.price - lo.price
    if rng <= 0:
        return None
    if lo.index < h.index:  # up-leg: retrace down from the high
        ret = {r: h.price - r * rng for r in RETRACEMENTS}
        ext = {e: lo.price + e * rng for e in EXTENSIONS}
        return FibLevels(lo.price, h.price, "up", ret, ext)
    ret = {r: lo.price + r * rng for r in RETRACEMENTS}
    ext = {e: h.price - e * rng for e in EXTENSIONS}
    return FibLevels(h.price, lo.price, "down", ret, ext)
