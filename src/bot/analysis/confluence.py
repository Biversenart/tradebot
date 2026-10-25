"""Confluence score 0-100 (spec §5.3.h). Each component scores 0..1; weights from config."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from bot.analysis.engine import MultiTimeframeAnalysis, TimeframeAnalysis
from bot.analysis.regime import Regime
from bot.analysis.structure import Direction
from bot.analysis.zones import Zone
from bot.config.schema import DEFAULT_CONFLUENCE_WEIGHTS, AnalysisConfig


@dataclass(frozen=True)
class Component:
    name: str
    score: float  # 0..1
    weight: float
    reason: str

    @property
    def points(self) -> float:
        return self.score * self.weight


@dataclass
class ConfluenceScore:
    direction: Direction
    components: list[Component]
    penalty: float = 1.0
    reasons: list[str] = field(default_factory=list)

    @property
    def raw(self) -> float:
        total_w = sum(c.weight for c in self.components) or 1.0
        return 100 * sum(c.points for c in self.components) / total_w

    @property
    def total(self) -> float:
        return round(self.raw * self.penalty, 2)

    def as_decimal(self) -> Decimal:
        return Decimal(str(self.total))


def nearest_zone(ta: TimeframeAnalysis, direction: Direction) -> Zone | None:
    """Closest zone on the side that supports `direction` (bullish zones for longs)."""
    price = ta.ind.close
    want_bull = direction is Direction.BULLISH
    cands = [z for z in ta.zones if z.kind.is_bullish == want_bull]
    if want_bull:
        cands = [z for z in cands if z.low <= price]
    else:
        cands = [z for z in cands if z.high >= price]
    return min(cands, key=lambda z: z.distance(price), default=None)


def _sign(d: Direction) -> int:
    return {Direction.BULLISH: 1, Direction.BEARISH: -1, Direction.NEUTRAL: 0}[d]


def score_setup(
    mtf: MultiTimeframeAnalysis,
    setup_tf: str,
    direction: Direction,
    cfg: AnalysisConfig | None = None,
) -> ConfluenceScore:
    if direction is Direction.NEUTRAL:
        raise ValueError("Yön long (bullish) veya short (bearish) olmalı.")
    cfg = cfg or AnalysisConfig()
    cc = cfg.confluence
    weights = {k: float(cc.weights.get(k, v)) for k, v in DEFAULT_CONFLUENCE_WEIGHTS.items()}
    ta = mtf.frames[setup_tf]
    sgn = _sign(direction)
    atr = ta.ind.atr
    recent = cc.recent_bars
    comps: list[Component] = []

    # 1) higher-timeframe trend alignment
    htf = mtf.htf_bias
    s = 1.0 if _sign(htf) == sgn else (0.5 if htf is Direction.NEUTRAL else 0.0)
    comps.append(
        Component(
            "htf_trend_alignment",
            s,
            weights["htf_trend_alignment"],
            f"üst zaman dilimi eğilimi: {htf.label_tr}",
        )
    )

    # 2) proximity to a supporting zone
    z = nearest_zone(ta, direction)
    prox = float(cfg.zones.proximity_atr) * atr
    if z is not None and z.distance(ta.ind.close) <= prox:
        s = 0.5 + 0.5 * min(z.strength, 100) / 100
        reason = f"{z.kind.label_tr} yakınında ({z.low:.6g}–{z.high:.6g}, güç {z.strength:.0f})"
    else:
        s, reason = 0.0, "destekleyici bölgeye yakın değil"
    comps.append(Component("zone_proximity", s, weights["zone_proximity"], reason))

    # 3) market structure
    ev = ta.structure.last_event
    st = ta.structure.trend
    if ev is not None and _sign(ev.direction) == sgn and ev.index > ta.last_index - 3 * recent:
        s, reason = 1.0, f"yakın zamanda {ev.kind} ({ev.direction.label_tr})"
    elif _sign(st) == sgn:
        s, reason = 0.75, f"yapı {st.label_tr}"
    elif st is Direction.NEUTRAL:
        s, reason = 0.4, "yapı nötr"
    else:
        s, reason = 0.0, f"yapı ters ({st.label_tr})"
    comps.append(Component("structure_confirmation", s, weights["structure_confirmation"], reason))

    # 4) volume
    vr = ta.ind.volume_ratio
    obv_ok = ta.ind.obv_slope is not None and ta.ind.obv_slope * sgn > 0
    if vr is not None and vr >= 1.2:
        s = 1.0 if obv_ok else 0.7
    elif vr is not None and vr >= 1.0:
        s = 0.6 if obv_ok else 0.4
    else:
        s = 0.3 if obv_ok else 0.0
    vr_txt = f"{vr:.2f}" if vr is not None else "-"
    obv_txt = "uyumlu" if obv_ok else "uyumsuz"
    comps.append(
        Component(
            "volume_confirmation",
            s,
            weights["volume_confirmation"],
            f"hacim/ortalama {vr_txt}, OBV {obv_txt}",
        )
    )

    # 5) patterns
    cps = [c for c in ta.recent_candles(recent) if _sign(c.direction) == sgn]
    charts = [c for c in ta.charts if _sign(c.direction) == sgn]
    if any(c.at_zone for c in cps) or any(c.confirmed for c in charts):
        s = 1.0
    elif cps or charts:
        s = 0.5
    else:
        s = 0.0
    names = [c.label_tr for c in cps] + [c.label_tr for c in charts]
    comps.append(
        Component(
            "pattern",
            s,
            weights["pattern"],
            "formasyon: " + (", ".join(dict.fromkeys(names)) or "yok"),
        )
    )

    # 6) divergence
    divs = [d for d in ta.recent_divergences(recent * 3) if (1 if d.kind.is_bullish else -1) == sgn]
    if any(d.kind.value.startswith("regular") for d in divs):
        s = 1.0
    elif divs:
        s = 0.7
    else:
        s = 0.0
    comps.append(
        Component(
            "divergence",
            s,
            weights["divergence"],
            "uyumsuzluk: "
            + (", ".join(f"{d.indicator.upper()} {d.kind.label_tr}" for d in divs) or "yok"),
        )
    )

    # 7) regime fit
    r = ta.regime.regime
    fits = {
        (Regime.TREND_UP, 1): 1.0,
        (Regime.TREND_DOWN, -1): 1.0,
        (Regime.TREND_UP, -1): 0.2,
        (Regime.TREND_DOWN, 1): 0.2,
    }
    if r is Regime.RANGE:
        s = 0.5
    elif r in (Regime.VOLATILE, Regime.UNKNOWN):
        s = 0.0
    else:
        s = fits[(r, sgn)]
    comps.append(Component("regime_fit", s, weights["regime_fit"], f"rejim: {r.label_tr}"))

    penalty = 1.0
    reasons = [c.reason for c in comps if c.score >= 0.5]
    if _sign(htf) == -sgn:
        penalty = float(cc.htf_conflict_penalty)
        reasons.append(f"UYARI: üst zaman dilimi tersine ({htf.label_tr}); puan x{penalty}")
    return ConfluenceScore(direction, comps, penalty, reasons)
