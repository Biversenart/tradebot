"""TradePlan generation (spec §5.3.i).

Long (short mirrored):
- Entry zone: nearest supporting zone (support/demand) within `proximity_atr` x ATR of price,
  clipped to price; otherwise a tight band just below price (market entry).
- Stop: below the nearest swing low under the entry zone (or the zone low), minus
  `sl_atr_buffer` x ATR. Stops farther than `max_stop_atr` x ATR are rejected.
- Targets: R multiples from `trade_plan.take_profits`, measured from the middle of the entry.
- Filters: R:R to the final target >= `min_risk_reward`; no opposing zone in the way of the
  minimum-R target; confluence >= `min_score`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from bot.analysis.confluence import ConfluenceScore, nearest_zone, score_setup
from bot.analysis.engine import MultiTimeframeAnalysis
from bot.analysis.structure import Direction, SwingKind
from bot.config.schema import AnalysisConfig
from bot.core.models import PositionSide, TradePlan


def to_price(x: float) -> Decimal:
    """Float -> Decimal with 8 significant digits (display/plan precision)."""
    return Decimal(f"{x:.8g}")


@dataclass
class PlanResult:
    direction: Direction
    timeframe: str
    score: ConfluenceScore
    plan: TradePlan | None
    rejected: str | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    targets: tuple[float, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.plan is not None


def build_plan(
    mtf: MultiTimeframeAnalysis,
    setup_tf: str,
    direction: Direction,
    cfg: AnalysisConfig | None = None,
    now: datetime | None = None,
) -> PlanResult:
    cfg = cfg or AnalysisConfig()
    tp_cfg = cfg.trade_plan
    ta = mtf.frames[setup_tf]
    score = score_setup(mtf, setup_tf, direction, cfg)
    price, atr = ta.ind.close, ta.ind.atr
    long = direction is Direction.BULLISH
    zone = nearest_zone(ta, direction)
    prox = float(cfg.zones.proximity_atr) * atr

    if zone is not None and zone.distance(price) <= prox:
        if long:
            e_lo, e_hi = zone.low, min(zone.high, price)
        else:
            e_lo, e_hi = max(zone.low, price), zone.high
    else:
        band = 0.25 * atr
        e_lo, e_hi = (price - band, price) if long else (price, price + band)
    if e_hi - e_lo < 1e-12:
        e_lo, e_hi = (e_lo - 0.1 * atr, e_hi) if long else (e_lo, e_hi + 0.1 * atr)

    buffer = float(tp_cfg.sl_atr_buffer) * atr
    if long:
        lows = [s.price for s in ta.structure.swings if s.kind is SwingKind.LOW and s.price < e_lo]
        level = max(lows) if lows else (zone.low if zone is not None else e_lo - atr)
        stop = min(level, e_lo) - buffer
    else:
        highs = [
            s.price for s in ta.structure.swings if s.kind is SwingKind.HIGH and s.price > e_hi
        ]
        level = highs[-1] if highs else (zone.high if zone is not None else e_hi + atr)
        stop = max(level, e_hi) + buffer

    mid = (e_lo + e_hi) / 2
    risk = abs(mid - stop)
    sgn = 1 if long else -1
    targets = tuple(mid + sgn * float(r) * risk for r in tp_cfg.take_profits)
    result = PlanResult(direction, setup_tf, score, None, None, e_lo, e_hi, stop, targets)

    rr = abs(targets[-1] - mid) / risk if risk > 0 else 0.0
    if risk <= 0:
        result.rejected = "risk hesaplanamadı (stop girişe çok yakın)"
        return result
    if risk > float(tp_cfg.max_stop_atr) * atr:
        result.rejected = f"stop çok uzak ({risk / atr:.1f} ATR > {tp_cfg.max_stop_atr})"
        return result
    if min(targets) <= 0:
        result.rejected = "hedef fiyat sıfırın altında"
        return result
    if rr < float(tp_cfg.min_risk_reward):
        result.rejected = f"R:R {rr:.2f} < {tp_cfg.min_risk_reward}"
        return result
    min_target = mid + sgn * float(tp_cfg.min_risk_reward) * risk
    blockers = [
        z
        for z in ta.zones
        if z.kind.is_bullish != long
        and z.strength >= 50
        and ((long and mid < z.low < min_target) or (not long and min_target < z.high < mid))
    ]
    if blockers:
        b = min(blockers, key=lambda z: abs(z.mid - mid))
        result.rejected = (
            f"{b.kind.label_tr} ({b.low:.6g}–{b.high:.6g}) minimum {tp_cfg.min_risk_reward}R "
            "hedefinin önünde; yetersiz alan"
        )
        return result
    if score.total < float(cfg.confluence.min_score):
        result.rejected = f"confluence {score.total:.1f} < {cfg.confluence.min_score}"
        return result

    try:
        plan = _make_plan(mtf, setup_tf, long, e_lo, e_hi, stop, targets, score, now)
    except ValueError as exc:  # rounding produced an inconsistent plan
        result.rejected = f"plan doğrulanamadı: {exc}"
        return result
    result.plan = plan
    return result


def _make_plan(
    mtf: MultiTimeframeAnalysis,
    setup_tf: str,
    long: bool,
    e_lo: float,
    e_hi: float,
    stop: float,
    targets: tuple[float, ...],
    score: ConfluenceScore,
    now: datetime | None,
) -> TradePlan:
    side = PositionSide.LONG if long else PositionSide.SHORT
    cmp = "altında" if long else "üstünde"
    return TradePlan(
        symbol=mtf.symbol,
        exchange=mtf.exchange,
        side=side,
        horizon=mtf.horizon_of(setup_tf),
        timeframe=setup_tf,
        entry_low=to_price(e_lo),
        entry_high=to_price(e_hi),
        stop_loss=to_price(stop),
        take_profits=tuple(to_price(t) for t in targets),
        confluence_score=score.as_decimal(),
        reasons=tuple(score.reasons),
        invalidation=f"{setup_tf} kapanışı {to_price(stop)} {cmp} veya ters yönde CHoCH",
        created_at=now or datetime.now(UTC),
    )


def best_plan(
    mtf: MultiTimeframeAnalysis, cfg: AnalysisConfig | None = None, now: datetime | None = None
) -> tuple[PlanResult | None, list[PlanResult]]:
    """Evaluate long and short on the setup timeframe; return (best accepted, all results)."""
    tf = mtf.setup_timeframe()
    if tf is None:
        return None, []
    results = [build_plan(mtf, tf, d, cfg, now) for d in (Direction.BULLISH, Direction.BEARISH)]
    accepted = [r for r in results if r.accepted]
    best = max(accepted, key=lambda r: r.score.total, default=None)
    return best, results
