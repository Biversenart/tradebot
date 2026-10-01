"""Per-timeframe and multi-timeframe analysis (spec §5.3.a).

`analyze_timeframe` runs every detector on one timeframe; `analyze_symbol` combines the
long / mid / short groups and derives the higher-timeframe bias used as a filter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from bot.analysis.divergence import Divergence, find_divergences
from bot.analysis.patterns import (
    CandlePattern,
    ChartPattern,
    candle_patterns,
    chart_patterns,
    mark_at_zone,
)
from bot.analysis.regime import RegimeState, detect_regime
from bot.analysis.structure import Direction, MarketStructure, analyze_structure, swing_trend
from bot.analysis.volume_profile import VolumeProfile, volume_profile
from bot.analysis.zones import (
    FibLevels,
    LiquidityPool,
    Zone,
    fibonacci,
    liquidity_pools,
    supply_demand,
    support_resistance,
)
from bot.config.schema import AnalysisConfig
from bot.core.models import Horizon
from bot.indicators import atr, ema, macd, obv, rsi, sma


@dataclass(frozen=True)
class IndicatorSnapshot:
    close: float
    atr: float
    rsi: float | None
    macd_hist: float | None
    adx: float | None
    emas: dict[int, float]
    volume_ratio: float | None  # last volume / volume SMA
    obv_slope: float | None


@dataclass
class TimeframeAnalysis:
    timeframe: str
    df: pd.DataFrame
    ind: IndicatorSnapshot
    regime: RegimeState
    structure: MarketStructure
    swing_trend: Direction
    bias: Direction
    zones: list[Zone]
    liquidity: list[LiquidityPool]
    fib: FibLevels | None
    profile: VolumeProfile | None
    divergences: list[Divergence]
    candles: list[CandlePattern]
    charts: list[ChartPattern]
    atr_series: pd.Series = field(repr=False)

    @property
    def last_index(self) -> int:
        return len(self.df) - 1

    def recent_candles(self, bars: int) -> list[CandlePattern]:
        return [c for c in self.candles if c.index > self.last_index - bars]

    def recent_divergences(self, bars: int) -> list[Divergence]:
        return [d for d in self.divergences if d.end_index > self.last_index - bars]


def _last(s: pd.Series) -> float | None:
    v = s.dropna()
    return float(v.iloc[-1]) if not v.empty else None


def _bias(
    close: float, emas: dict[int, float], structure: Direction, regime: RegimeState
) -> Direction:
    score = 0
    e50, e200 = emas.get(50), emas.get(200)
    if e200 is not None:
        score += 1 if close > e200 else -1
    if e50 is not None and e200 is not None:
        score += 1 if e50 > e200 else -1
    score += {Direction.BULLISH: 1, Direction.BEARISH: -1, Direction.NEUTRAL: 0}[structure]
    if regime.regime.value == "trend_up":
        score += 1
    elif regime.regime.value == "trend_down":
        score -= 1
    if score >= 2:
        return Direction.BULLISH
    if score <= -2:
        return Direction.BEARISH
    return Direction.NEUTRAL


def analyze_timeframe(
    df: pd.DataFrame, timeframe: str, cfg: AnalysisConfig | None = None
) -> TimeframeAnalysis:
    cfg = cfg or AnalysisConfig()
    ip = cfg.indicators
    if len(df) < 30:
        raise ValueError(f"{timeframe}: analiz için en az 30 mum gerekli (var: {len(df)}).")
    df = df.iloc[-cfg.analysis_bars :]
    close = df["close"]
    atr_s = atr(df, ip.atr_period)
    atr_v = _last(atr_s) or float((df["high"] - df["low"]).mean())
    rsi_s = rsi(close, ip.rsi_period)
    m = macd(close, ip.macd_fast, ip.macd_slow, ip.macd_signal)
    emas = {n: v for n in ip.ema_periods if (v := _last(ema(close, n))) is not None}
    vol_sma = sma(df["volume"], ip.volume_sma)
    vr = None
    if (vs := _last(vol_sma)) and vs > 0:
        vr = float(df["volume"].iloc[-1]) / vs
    ob = obv(df)
    obv_slope = float(ob.iloc[-1] - ob.iloc[-10]) if len(ob) >= 10 else None

    regime = detect_regime(df, cfg.regime, ip)
    structure = analyze_structure(df, cfg.structure.swing_lookback)
    zones = support_resistance(df, structure.swings, atr_v, cfg.zones) + supply_demand(
        df, atr_s, cfg.zones
    )
    pad = float(cfg.zones.proximity_atr) * atr_v * 0.25
    candles = mark_at_zone(candle_patterns(df, cfg.patterns), df, zones, pad)
    vp_df = df.iloc[-cfg.volume_profile.lookback_bars :]
    profile = volume_profile(
        vp_df, cfg.volume_profile.bins, float(cfg.volume_profile.value_area_pct)
    )
    divs = find_divergences(structure.swings, rsi_s, "rsi", cfg.divergence) + find_divergences(
        structure.swings, m.hist, "macd", cfg.divergence
    )
    snapshot = IndicatorSnapshot(
        close=float(close.iloc[-1]),
        atr=atr_v,
        rsi=_last(rsi_s),
        macd_hist=_last(m.hist),
        adx=regime.adx,
        emas=emas,
        volume_ratio=vr,
        obv_slope=obv_slope,
    )
    st = swing_trend(structure)
    trend = structure.trend if structure.trend is not Direction.NEUTRAL else st
    return TimeframeAnalysis(
        timeframe=timeframe,
        df=df,
        ind=snapshot,
        regime=regime,
        structure=structure,
        swing_trend=st,
        bias=_bias(snapshot.close, emas, trend, regime),
        zones=zones,
        liquidity=liquidity_pools(df, structure.swings, atr_v, cfg.zones),
        fib=fibonacci(structure.swings),
        profile=profile,
        divergences=sorted(divs, key=lambda d: d.end_index),
        candles=candles,
        charts=chart_patterns(df, structure.swings, atr_s, cfg.patterns),
        atr_series=atr_s,
    )


GROUP_HORIZON = {"long": Horizon.POSITION, "mid": Horizon.SWING, "short": Horizon.SCALP}


@dataclass
class MultiTimeframeAnalysis:
    symbol: str
    exchange: str
    frames: dict[str, TimeframeAnalysis]
    groups: dict[str, list[str]]
    missing: list[str]

    def group_bias(self, group: str) -> Direction:
        """Majority of available timeframes in the group (higher TF breaks ties)."""
        tfs = [tf for tf in self.groups.get(group, []) if tf in self.frames]
        if not tfs:
            return Direction.NEUTRAL
        votes = [self.frames[tf].bias for tf in tfs]
        bull, bear = votes.count(Direction.BULLISH), votes.count(Direction.BEARISH)
        if bull > bear:
            return Direction.BULLISH
        if bear > bull:
            return Direction.BEARISH
        return votes[0]

    @property
    def htf_bias(self) -> Direction:
        return self.group_bias("long")

    def setup_timeframe(self) -> str | None:
        for group in ("mid", "short", "long"):
            for tf in self.groups.get(group, []):
                if tf in self.frames:
                    return tf
        return None

    def horizon_of(self, tf: str) -> Horizon:
        for group, tfs in self.groups.items():
            if tf in tfs:
                return GROUP_HORIZON[group]
        return Horizon.SWING


def analyze_symbol(
    frames: dict[str, pd.DataFrame],
    symbol: str,
    exchange: str = "binance",
    cfg: AnalysisConfig | None = None,
) -> MultiTimeframeAnalysis:
    cfg = cfg or AnalysisConfig()
    groups = {
        "long": list(cfg.timeframes.long),
        "mid": list(cfg.timeframes.mid),
        "short": list(cfg.timeframes.short),
    }
    out: dict[str, TimeframeAnalysis] = {}
    missing: list[str] = []
    for tfs in groups.values():
        for tf in tfs:
            df = frames.get(tf)
            if df is None or len(df) < 30:
                missing.append(tf)
                continue
            out[tf] = analyze_timeframe(df, tf, cfg)
    return MultiTimeframeAnalysis(symbol, exchange, out, groups, missing)
