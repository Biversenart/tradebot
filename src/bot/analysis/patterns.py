"""Candlestick and chart patterns (spec §5.3.f). Geometric detection with ATR-based tolerances.

Candle patterns are only *meaningful* near an important zone; `at_zone` marks that.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from bot.analysis.structure import Direction, SwingKind, SwingPoint
from bot.analysis.zones import Zone
from bot.config.schema import PatternParams

CANDLE_LABELS_TR = {
    "doji": "doji",
    "bullish_engulfing": "yükseliş yutan",
    "bearish_engulfing": "düşüş yutan",
    "hammer": "çekiç (pin bar)",
    "shooting_star": "kayan yıldız (pin bar)",
    "inside_bar": "iç bar",
    "morning_star": "sabah yıldızı",
    "evening_star": "akşam yıldızı",
}

CHART_LABELS_TR = {
    "double_top": "çift tepe",
    "double_bottom": "çift dip",
    "head_shoulders": "omuz-baş-omuz (OBO)",
    "inverse_head_shoulders": "ters omuz-baş-omuz (TOBO)",
    "ascending_triangle": "yükselen üçgen",
    "descending_triangle": "alçalan üçgen",
    "symmetrical_triangle": "simetrik üçgen",
    "bull_flag": "boğa bayrağı",
    "bear_flag": "ayı bayrağı",
    "channel_breakout_up": "kanal kırılımı (yukarı)",
    "channel_breakout_down": "kanal kırılımı (aşağı)",
}


@dataclass(frozen=True)
class CandlePattern:
    index: int
    name: str
    direction: Direction
    at_zone: bool = False

    @property
    def label_tr(self) -> str:
        return CANDLE_LABELS_TR[self.name]


@dataclass(frozen=True)
class ChartPattern:
    name: str
    direction: Direction
    start_index: int
    end_index: int
    confirmed: bool
    levels: dict[str, float] = field(default_factory=dict)

    @property
    def label_tr(self) -> str:
        return CHART_LABELS_TR[self.name]


# --------------------------------------------------------------------------- candles


def candle_patterns(df: pd.DataFrame, params: PatternParams | None = None) -> list[CandlePattern]:
    p = params or PatternParams()
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    body = np.abs(c - o)
    rng = h - lo
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - lo
    ratio = float(p.pin_wick_body_ratio)
    out: list[CandlePattern] = []
    for i in range(len(df)):
        if rng[i] <= 0:
            continue
        if body[i] <= float(p.doji_body_pct) * rng[i]:
            out.append(CandlePattern(i, "doji", Direction.NEUTRAL))
        else:
            if lower[i] >= ratio * body[i] and upper[i] <= body[i]:
                out.append(CandlePattern(i, "hammer", Direction.BULLISH))
            if upper[i] >= ratio * body[i] and lower[i] <= body[i]:
                out.append(CandlePattern(i, "shooting_star", Direction.BEARISH))
        if i >= 1:
            prev_bear, prev_bull = c[i - 1] < o[i - 1], c[i - 1] > o[i - 1]
            if (
                prev_bear
                and c[i] > o[i]
                and o[i] <= c[i - 1]
                and c[i] >= o[i - 1]
                and body[i] > body[i - 1]
            ):
                out.append(CandlePattern(i, "bullish_engulfing", Direction.BULLISH))
            if (
                prev_bull
                and c[i] < o[i]
                and o[i] >= c[i - 1]
                and c[i] <= o[i - 1]
                and body[i] > body[i - 1]
            ):
                out.append(CandlePattern(i, "bearish_engulfing", Direction.BEARISH))
            if h[i] < h[i - 1] and lo[i] > lo[i - 1]:
                out.append(CandlePattern(i, "inside_bar", Direction.NEUTRAL))
        if i >= 2:
            b1, b2 = body[i - 2], body[i - 1]
            small = b2 <= 0.3 * b1
            mid1 = (o[i - 2] + c[i - 2]) / 2
            if (
                c[i - 2] < o[i - 2]
                and small
                and max(o[i - 1], c[i - 1]) <= c[i - 2]
                and c[i] > o[i]
                and c[i] > mid1
            ):
                out.append(CandlePattern(i, "morning_star", Direction.BULLISH))
            if (
                c[i - 2] > o[i - 2]
                and small
                and min(o[i - 1], c[i - 1]) >= c[i - 2]
                and c[i] < o[i]
                and c[i] < mid1
            ):
                out.append(CandlePattern(i, "evening_star", Direction.BEARISH))
    return out


def mark_at_zone(
    patterns: list[CandlePattern], df: pd.DataFrame, zones: list[Zone], pad: float
) -> list[CandlePattern]:
    """Flag candle patterns whose bar touches a zone (± pad)."""
    h = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    out = []
    for cp in patterns:
        hit = any(lo[cp.index] <= z.high + pad and h[cp.index] >= z.low - pad for z in zones)
        out.append(replace(cp, at_zone=hit))
    return out


# --------------------------------------------------------------------------- chart patterns


def _of(swings: list[SwingPoint], kind: SwingKind) -> list[SwingPoint]:
    return [s for s in swings if s.kind is kind]


def _closes_after(df: pd.DataFrame, start: int) -> np.ndarray:
    return df["close"].to_numpy(dtype=float)[start + 1 :]


def double_patterns(
    df: pd.DataFrame, swings: list[SwingPoint], atr_value: float, p: PatternParams
) -> list[ChartPattern]:
    tol = float(p.level_tolerance_atr) * atr_value
    out: list[ChartPattern] = []
    highs, lows = _of(swings, SwingKind.HIGH), _of(swings, SwingKind.LOW)
    if len(highs) >= 2:
        h1, h2 = highs[-2], highs[-1]
        between = [s for s in lows if h1.index < s.index < h2.index]
        if (
            between
            and abs(h1.price - h2.price) <= tol
            and h2.index - h1.index >= p.min_pattern_bars
        ):
            neck = min(s.price for s in between)
            if max(h1.price, h2.price) - neck > 2 * tol:
                confirmed = bool((_closes_after(df, h2.index) < neck).any())
                out.append(
                    ChartPattern(
                        "double_top",
                        Direction.BEARISH,
                        h1.index,
                        h2.index,
                        confirmed,
                        {"neckline": neck, "top": max(h1.price, h2.price)},
                    )
                )
    if len(lows) >= 2:
        l1, l2 = lows[-2], lows[-1]
        between = [s for s in highs if l1.index < s.index < l2.index]
        if (
            between
            and abs(l1.price - l2.price) <= tol
            and l2.index - l1.index >= p.min_pattern_bars
        ):
            neck = max(s.price for s in between)
            if neck - min(l1.price, l2.price) > 2 * tol:
                confirmed = bool((_closes_after(df, l2.index) > neck).any())
                out.append(
                    ChartPattern(
                        "double_bottom",
                        Direction.BULLISH,
                        l1.index,
                        l2.index,
                        confirmed,
                        {"neckline": neck, "bottom": min(l1.price, l2.price)},
                    )
                )
    return out


def head_shoulders(
    df: pd.DataFrame, swings: list[SwingPoint], atr_value: float, p: PatternParams
) -> list[ChartPattern]:
    tol = float(p.level_tolerance_atr) * atr_value
    out: list[ChartPattern] = []
    for kind, name, direction in (
        (SwingKind.HIGH, "head_shoulders", Direction.BEARISH),
        (SwingKind.LOW, "inverse_head_shoulders", Direction.BULLISH),
    ):
        pts = _of(swings, kind)
        if len(pts) < 3:
            continue
        s1, hd, s2 = pts[-3], pts[-2], pts[-1]
        sign = 1 if kind is SwingKind.HIGH else -1
        if not (sign * (hd.price - s1.price) > tol and sign * (hd.price - s2.price) > tol):
            continue
        if abs(s1.price - s2.price) > 2 * tol:
            continue
        other = SwingKind.LOW if kind is SwingKind.HIGH else SwingKind.HIGH
        neck_pts = [s for s in _of(swings, other) if s1.index < s.index < s2.index]
        if len(neck_pts) < 1:
            continue
        neck = float(np.mean([s.price for s in neck_pts]))
        after = _closes_after(df, s2.index)
        confirmed = bool((after < neck).any() if sign == 1 else (after > neck).any())
        out.append(
            ChartPattern(
                name, direction, s1.index, s2.index, confirmed, {"neckline": neck, "head": hd.price}
            )
        )
    return out


def _line(points: list[SwingPoint]) -> tuple[float, float]:
    x = np.array([s.index for s in points], dtype=float)
    y = np.array([s.price for s in points], dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    return float(slope), float(intercept)


def triangles(
    df: pd.DataFrame, swings: list[SwingPoint], atr_value: float, p: PatternParams
) -> list[ChartPattern]:
    highs, lows = _of(swings, SwingKind.HIGH)[-3:], _of(swings, SwingKind.LOW)[-3:]
    if len(highs) < 2 or len(lows) < 2:
        return []
    start = min(highs[0].index, lows[0].index)
    end = max(highs[-1].index, lows[-1].index)
    if end - start < p.min_pattern_bars:
        return []
    hs, hi_icpt = _line(highs)
    ls, lo_icpt = _line(lows)
    flat = float(p.triangle_flat_slope_atr) * atr_value
    width_start = (hs * start + hi_icpt) - (ls * start + lo_icpt)
    width_end = (hs * end + hi_icpt) - (ls * end + lo_icpt)
    if width_start <= 0 or width_end >= width_start * 0.8:
        return []  # not converging
    if abs(hs) <= flat and ls > flat:
        name, direction = "ascending_triangle", Direction.BULLISH
    elif abs(ls) <= flat and hs < -flat:
        name, direction = "descending_triangle", Direction.BEARISH
    elif hs < -flat and ls > flat:
        name, direction = "symmetrical_triangle", Direction.NEUTRAL
    else:
        return []
    last = len(df) - 1
    upper_now = hs * last + hi_icpt
    lower_now = ls * last + lo_icpt
    close = float(df["close"].iloc[-1])
    confirmed = close > upper_now or close < lower_now
    if confirmed and direction is Direction.NEUTRAL:
        direction = Direction.BULLISH if close > upper_now else Direction.BEARISH
    return [
        ChartPattern(
            name, direction, start, end, confirmed, {"upper": upper_now, "lower": lower_now}
        )
    ]


def flags(
    df: pd.DataFrame, atr: pd.Series, p: PatternParams, pole_bars: int = 10, flag_bars: int = 8
) -> list[ChartPattern]:
    if len(df) < pole_bars + flag_bars + 1:
        return []
    c = df["close"].to_numpy(dtype=float)
    a = float(atr.iloc[-flag_bars - 1]) if not np.isnan(atr.iloc[-flag_bars - 1]) else 0.0
    if a <= 0:
        return []
    pole_start, pole_end = len(c) - flag_bars - pole_bars - 1, len(c) - flag_bars - 1
    pole = c[pole_end] - c[pole_start]
    if abs(pole) < float(p.flag_pole_atr) * a:
        return []
    flag = df.iloc[pole_end + 1 :]
    f_range = float(flag["high"].max() - flag["low"].min())
    if f_range > 0.5 * abs(pole):
        return []
    drift = float(flag["close"].iloc[-1] - flag["close"].iloc[0])
    if pole > 0 and drift <= 0.25 * a:  # bull flag: flat/slightly down consolidation
        upper = float(flag["high"].max())
        return [
            ChartPattern(
                "bull_flag",
                Direction.BULLISH,
                pole_start,
                len(c) - 1,
                bool(c[-1] >= upper),
                {"pole": pole, "upper": upper},
            )
        ]
    if pole < 0 and drift >= -0.25 * a:
        lower = float(flag["low"].min())
        return [
            ChartPattern(
                "bear_flag",
                Direction.BEARISH,
                pole_start,
                len(c) - 1,
                bool(c[-1] <= lower),
                {"pole": pole, "lower": lower},
            )
        ]
    return []


def channel_breakout(df: pd.DataFrame, p: PatternParams) -> list[ChartPattern]:
    n = p.channel_lookback
    if len(df) < n + 1:
        return []
    closes = df["close"].to_numpy(dtype=float)
    window = closes[-n - 1 : -1]
    x = np.arange(n, dtype=float)
    slope, icpt = np.polyfit(x, window, 1)
    resid = window - (slope * x + icpt)
    std = float(resid.std())
    if std <= 0:
        return []
    fit_now = slope * n + icpt
    k = float(p.channel_std)
    upper, lower = fit_now + k * std, fit_now - k * std
    last = closes[-1]
    start = len(df) - n - 1
    if last > upper:
        return [
            ChartPattern(
                "channel_breakout_up",
                Direction.BULLISH,
                start,
                len(df) - 1,
                True,
                {"upper": upper, "lower": lower},
            )
        ]
    if last < lower:
        return [
            ChartPattern(
                "channel_breakout_down",
                Direction.BEARISH,
                start,
                len(df) - 1,
                True,
                {"upper": upper, "lower": lower},
            )
        ]
    return []


def chart_patterns(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    atr: pd.Series,
    params: PatternParams | None = None,
) -> list[ChartPattern]:
    p = params or PatternParams()
    a = atr.dropna()
    if a.empty:
        return []
    atr_value = float(a.iloc[-1])
    return [
        *double_patterns(df, swings, atr_value, p),
        *head_shoulders(df, swings, atr_value, p),
        *triangles(df, swings, atr_value, p),
        *flags(df, atr, p),
        *channel_breakout(df, p),
    ]
