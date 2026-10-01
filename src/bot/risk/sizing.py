"""Growth-oriented position sizing (spec §5.6). Formulas documented in docs/KARARLAR.md.

risk_pct = base x quality x drawdown x loss_streak x event x allocation   (Kelly-capped)
         and never above base x quality.high_score (hard ceiling)
qty      = tradable_equity x risk_pct / 100 / |entry - stop|
tradable_equity = (compounding ? current equity : initial equity) - locked profit reserve

Volatility adjustment is inherent: stops are ATR/structure based, so a wider (more volatile)
stop gives a smaller position for the same risk %.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from bot.config.schema import RiskConfig
from bot.core.models import Signal
from bot.risk.performance import ClosedTrade, PerformanceTracker

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass
class ProfitLock:
    """Every +every_gain_pct over the last lock level, lock lock_pct of that gain (reserve)."""

    every_gain_pct: Decimal
    lock_pct: Decimal
    level: Decimal  # equity at the last lock step
    reserve: Decimal = ZERO

    def update(self, equity: Decimal) -> None:
        step = self.level * (1 + self.every_gain_pct / 100)
        while equity >= step > 0:
            gain = step - self.level
            self.reserve += gain * self.lock_pct / 100
            self.level = step
            step = self.level * (1 + self.every_gain_pct / 100)


@dataclass
class SizingBreakdown:
    base: Decimal
    quality: Decimal = ONE
    drawdown: Decimal = ONE
    loss_streak: Decimal = ONE
    event: Decimal = ONE
    allocation: Decimal = ONE
    kelly_cap: Decimal | None = None
    final_risk_pct: Decimal = ZERO
    tradable_equity: Decimal = ZERO
    paused_reason: str | None = None
    notes: list[str] = field(default_factory=list)


class GrowthSizer:
    def __init__(
        self,
        risk: RiskConfig,
        initial_equity: Decimal,
        max_notional_pct: Decimal | None = None,
        tracker: PerformanceTracker | None = None,
    ) -> None:
        self.risk = risk
        g = risk.growth
        self.initial_equity = initial_equity
        self.peak = initial_equity
        self.max_notional_pct = max_notional_pct or risk.max_exposure_per_symbol_pct * 5
        self.tracker = tracker or PerformanceTracker(g.auto_allocation.lookback_trades)
        self.lock = (
            ProfitLock(g.profit_lock.every_gain_pct, g.profit_lock.lock_pct, initial_equity)
            if g.profit_lock.enabled
            else None
        )
        self.last: SizingBreakdown | None = None
        self._paused_until: dict[tuple[str, str], datetime] = {}

    # ---------------------------------------------------------------- state updates
    def on_equity(self, equity: Decimal) -> None:
        self.peak = max(self.peak, equity)
        if self.lock is not None:
            self.lock.update(equity)

    def on_trade_closed(
        self, strategy: str, symbol: str, pnl: Decimal, r_multiple: Decimal
    ) -> None:
        self.tracker.record(ClosedTrade(strategy, symbol, pnl, r_multiple))

    @property
    def reserve(self) -> Decimal:
        return self.lock.reserve if self.lock is not None else ZERO

    # ---------------------------------------------------------------- factors
    def quality_factor(self, score: Decimal) -> Decimal:
        q = self.risk.growth.quality_multiplier
        if score >= q.high_threshold:
            return q.high_score
        if score < q.low_threshold:
            return q.low_score
        return ONE

    def drawdown_factor(self, equity: Decimal) -> Decimal:
        ds = self.risk.growth.drawdown_scaling
        self.peak = max(self.peak, equity)
        if self.peak <= 0:
            return ONE
        dd = max(ZERO, (self.peak - equity) / self.peak * 100)
        if dd >= ds.start_pct:
            return ds.risk_factor
        # gradual recovery towards 1 as the drawdown shrinks back to 0
        return ds.risk_factor + (ONE - ds.risk_factor) * (ONE - dd / ds.start_pct)

    def loss_streak_factor(self) -> Decimal:
        ls = self.risk.growth.loss_streak
        return ls.risk_factor if self.tracker.loss_streak >= ls.count else ONE

    def event_factor(self, symbol: str, when: datetime | None) -> tuple[Decimal, str | None]:
        if when is None:
            return ONE, None
        for w in self.risk.event_windows:
            if w.start <= when <= w.end and (not w.symbols or symbol in w.symbols):
                return w.risk_factor, w.label
        return ONE, None

    def allocation_factor(
        self, strategy: str, symbol: str, when: datetime | None = None
    ) -> tuple[Decimal, str | None]:
        aa = self.risk.growth.auto_allocation
        if not aa.enabled:
            return ONE, None
        key = (strategy, symbol)
        until = self._paused_until.get(key)
        if until is not None and when is not None:
            if when < until:
                return ZERO, f"{strategy}/{symbol} {until:%Y-%m-%d %H:%M} UTC'ye kadar duraklatıldı"
            # cool-down over: fresh trial with an empty window
            del self._paused_until[key]
            self.tracker.reset(strategy, symbol)
            return ONE, None
        st = self.tracker.recent(strategy, symbol)
        if st.trades < aa.min_trades:
            return ONE, None
        if st.expectancy_r < aa.pause_if_expectancy_below:
            if when is not None:
                self._paused_until[key] = when + timedelta(hours=aa.pause_hours)
            return (
                ZERO,
                f"{strategy}/{symbol} son {st.trades} işlemde beklenti "
                f"{st.expectancy_r:.2f}R: duraklatıldı",
            )
        # positive expectancy: up to max_multiplier (expectancy 0.5R or more = full bonus)
        bonus = min(ONE, st.expectancy_r / Decimal("0.5")) * (aa.max_multiplier - ONE)
        return ONE + max(ZERO, bonus), None

    def kelly_cap_pct(self, strategy: str) -> Decimal | None:
        k = self.risk.growth.kelly
        if not k.enabled:
            return None
        st = self.tracker.strategy(strategy)
        if st.trades < k.min_trades or st.avg_loss_r <= 0 or st.avg_win_r <= 0:
            return None
        payoff = st.avg_win_r / st.avg_loss_r
        f = st.win_rate - (ONE - st.win_rate) / payoff  # Kelly fraction of equity at risk
        return max(ZERO, f * k.fraction * 100)

    # ---------------------------------------------------------------- sizing
    def risk_pct(self, signal: Signal, equity: Decimal) -> SizingBreakdown:
        r = self.risk
        b = SizingBreakdown(base=r.risk_per_trade_pct)
        b.quality = self.quality_factor(signal.score)
        b.drawdown = self.drawdown_factor(equity)
        b.loss_streak = self.loss_streak_factor()
        b.event, label = self.event_factor(signal.symbol, signal.timestamp)
        if label:
            b.notes.append(f"olay penceresi: {label}")
        b.allocation, paused = self.allocation_factor(
            signal.strategy, signal.symbol, signal.timestamp
        )
        if paused:
            b.paused_reason = paused
        pct = b.base * b.quality * b.drawdown * b.loss_streak * b.event * b.allocation
        ceiling = b.base * r.growth.quality_multiplier.high_score
        b.kelly_cap = self.kelly_cap_pct(signal.strategy)
        if b.kelly_cap is not None:
            if b.kelly_cap <= 0:
                b.paused_reason = b.paused_reason or f"{signal.strategy}: Kelly ≤ 0 (negatif kenar)"
            pct = min(pct, b.kelly_cap)
        b.final_risk_pct = max(ZERO, min(pct, ceiling))
        base_equity = equity if r.growth.compounding else min(equity, self.initial_equity)
        b.tradable_equity = max(ZERO, base_equity - self.reserve)
        self.last = b
        return b

    def size(self, equity: Decimal, entry: Decimal, stop: Decimal, signal: Signal) -> Decimal:
        risk_unit = abs(entry - stop)
        if risk_unit <= 0 or equity <= 0:
            return ZERO
        b = self.risk_pct(signal, equity)
        if b.paused_reason or b.final_risk_pct <= 0:
            return ZERO
        qty = b.tradable_equity * b.final_risk_pct / 100 / risk_unit
        cap = b.tradable_equity * self.max_notional_pct / 100 / entry
        return min(qty, cap)
