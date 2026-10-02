"""RiskManager (CLAUDE.md rule 4, spec §5.6): EVERY order passes through here.

The execution engine only accepts `ApprovedIntent`, which can only be constructed by this
module (a private token is checked), so nothing can reach an exchange without approval.

Entry checks, in order: egress gate -> kill switch -> ops blocks (depeg, clock skew,
delisting/maintenance) -> plan/stop sanity -> open-position limit -> spread -> growth sizing
(on canary-capped equity in live) -> low-liquidity factor -> correlation -> exposure / leverage
caps -> precision & min notional -> liquidity (order book depth). Exits (reduce-only) are
always allowed, even under the kill switch, but may never exceed the position.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import pandas as pd

from bot.config.schema import AppConfig
from bot.core.models import (
    MarketInfo,
    OrderBook,
    OrderIntent,
    OrderType,
    PositionSide,
    Side,
    Signal,
    Ticker,
)
from bot.core.precision import meets_min_notional, round_amount
from bot.indicators import atr_pct
from bot.ops.guard import OpsGuard
from bot.risk.kill_switch import KillSwitch
from bot.risk.portfolio import PortfolioView, returns_frame
from bot.risk.report import (
    Decision,
    RiskReport,
    classify,
    depth_within,
    expected_slippage,
    realized_vol,
)
from bot.risk.sizing import GrowthSizer

ZERO = Decimal(0)
_TOKEN = object()


class ApprovedIntent:
    """An OrderIntent approved (and sized) by RiskManager. Not constructible elsewhere."""

    __slots__ = ("approved_at", "intent", "report")

    def __init__(
        self,
        intent: OrderIntent,
        report: RiskReport | None,
        approved_at: datetime,
        _token: object = None,
    ) -> None:
        if _token is not _TOKEN:
            raise PermissionError("ApprovedIntent yalnızca RiskManager tarafından oluşturulabilir.")
        self.intent = intent
        self.report = report
        self.approved_at = approved_at

    def __repr__(self) -> str:
        i = self.intent
        return f"ApprovedIntent({i.symbol} {i.side} {i.amount} {i.order_type})"


@dataclass
class MarketContext:
    market: MarketInfo | None = None
    ticker: Ticker | None = None
    book: OrderBook | None = None
    candles: pd.DataFrame | None = None  # setup timeframe OHLCV for ATR / volatility
    closes: dict[str, pd.Series] = field(default_factory=dict)  # for correlation
    bars_per_day: float = 24.0


@dataclass
class RiskDecision:
    approved: ApprovedIntent | None
    report: RiskReport

    @property
    def ok(self) -> bool:
        return self.approved is not None


class RiskManager:
    def __init__(
        self,
        config: AppConfig,
        kill_switch: KillSwitch,
        sizer: GrowthSizer,
        order_gate: Callable[[], bool] = lambda: True,
        market_type: str = "spot",
        ops: OpsGuard | None = None,
    ) -> None:
        self.cfg = config
        self.ops = ops
        self.risk = config.risk
        self.kill_switch = kill_switch
        self.sizer = sizer
        self.order_gate = order_gate
        self.market_type = market_type

    # ---------------------------------------------------------------- helpers
    def _reject(self, report: RiskReport, reason: str) -> RiskDecision:
        report.decision = Decision.REJECT
        report.reasons.append(reason)
        report.qty = ZERO
        return RiskDecision(None, report)

    def _approve(self, intent: OrderIntent, report: RiskReport) -> RiskDecision:
        approved = ApprovedIntent(intent, report, self.kill_switch.clock.now(), _token=_TOKEN)
        return RiskDecision(approved, report)

    # ---------------------------------------------------------------- entries
    def assess_signal(
        self, signal: Signal, portfolio: PortfolioView, ctx: MarketContext
    ) -> RiskDecision:
        plan = signal.plan
        long = signal.side is PositionSide.LONG
        entry = plan.entry_high if plan and long else (plan.entry_low if plan else ZERO)
        if ctx.ticker is not None:
            entry = ctx.ticker.ask if long else ctx.ticker.bid
        stop = plan.stop_loss if plan else ZERO
        report = RiskReport(signal.symbol, signal.strategy, signal.side.value, entry, stop)

        if not self.order_gate():
            return self._reject(report, "egress doğrulanmadı (fail-closed)")
        if not self.kill_switch.allows_new_orders():
            reasons = ", ".join(r.value for r in self.kill_switch.active_reasons)
            return self._reject(report, f"kill switch aktif: {reasons}")
        if self.ops is not None and (
            block := self.ops.block_reason(signal.exchange, signal.symbol)
        ):
            return self._reject(report, block)
        if plan is None:
            return self._reject(report, "TradePlan yok")
        if (long and stop >= entry) or (not long and stop <= entry):
            return self._reject(report, "stop-loss giriş fiyatının yanlış tarafında")
        if not long and self.market_type == "spot":
            return self._reject(report, "spot piyasada açığa satış yok")
        if len(portfolio.positions) >= self.risk.max_open_positions:
            return self._reject(report, f"maksimum açık pozisyon ({self.risk.max_open_positions})")
        if ctx.ticker is not None and ctx.ticker.spread_pct > self.risk.max_spread_pct:
            return self._reject(
                report, f"spread %{ctx.ticker.spread_pct:.3f} > %{self.risk.max_spread_pct}"
            )

        equity = portfolio.equity
        qty = self.sizer.size(equity, entry, stop, signal)
        if self.ops is not None:
            # canary cap: scale AFTER sizing so the sizer's drawdown/compounding logic still
            # sees the real account; exposure caps below use the capped equity
            capped = self.ops.effective_equity(equity)
            if 0 < capped < equity:
                qty = qty * capped / equity
                report.reasons.append(f"kanarya sermaye tavanı: {capped:.2f} / {equity:.2f}")
            equity = capped
        sb = self.sizer.last
        if sb is not None:
            report.risk_pct = sb.final_risk_pct
            report.event = next((n for n in sb.notes if n.startswith("olay")), None)
            if sb.paused_reason:
                return self._reject(report, sb.paused_reason)
        if qty <= 0:
            return self._reject(report, "boyutlama sıfır miktar verdi")
        if self.ops is not None:
            factor, notes = self.ops.risk_factor(self.kill_switch.clock.now(), ctx.book)
            if factor <= 0:
                return self._reject(
                    report, "düşük likidite modu: yeni işlem kapalı (" + ", ".join(notes) + ")"
                )
            if factor < 1:
                qty *= factor
                report.reasons.append(f"düşük likidite ({', '.join(notes)}): boyut ×{factor}")
        report.requested_qty = qty

        # correlation with open positions in the same direction
        if ctx.closes and portfolio.positions:
            rets = returns_frame(ctx.closes, self.risk.correlation_lookback_bars)
            if signal.symbol in rets:
                same_dir = [
                    p
                    for p in portfolio.positions
                    if p.symbol != signal.symbol and p.side is signal.side and p.symbol in rets
                ]
                corrs = {
                    p.symbol: float(rets[signal.symbol].corr(rets[p.symbol])) for p in same_dir
                }
                high = [
                    s for s, c in corrs.items() if c == c and c >= float(self.risk.max_correlation)
                ]
                report.max_correlation = max(corrs.values(), default=None)
                report.correlated_with = high
                if len(high) >= self.risk.max_correlated_positions:
                    return self._reject(report, f"yüksek korelasyon: {', '.join(high)}")
                if high:
                    qty *= self.risk.correlation_reduce_factor
                    report.reasons.append(
                        f"{', '.join(high)} ile korelasyon ≥ {self.risk.max_correlation}: boyut "
                        f"×{self.risk.correlation_reduce_factor}"
                    )

        # exposure caps (per symbol / total / leverage)
        sym_cap = equity * self.risk.max_exposure_per_symbol_pct / 100 - portfolio.exposure(
            signal.symbol
        )
        tot_cap = equity * self.risk.max_total_exposure_pct / 100 - portfolio.exposure()
        lev_cap = equity * self.risk.max_leverage - portfolio.exposure()
        room = min(sym_cap, tot_cap, lev_cap)
        if room <= 0:
            return self._reject(report, "maruziyet limiti dolu")
        if qty * entry > room:
            qty = room / entry
            report.reasons.append("maruziyet limitine göre küçültüldü")

        # liquidity: depth within 0.5% must cover min_depth_ratio x notional
        side = Side.BUY if long else Side.SELL
        if ctx.book is not None:
            depth = depth_within(ctx.book, side)
            need = qty * entry * self.risk.min_depth_ratio
            report.liquidity_score = float(min(Decimal(1), depth / need)) if need > 0 else 1.0
            if depth <= 0:
                return self._reject(report, "orderbook derinliği yok")
            if depth < need:
                qty = depth / self.risk.min_depth_ratio / entry
                report.reasons.append("likiditeye göre küçültüldü")

        # precision / min notional
        if ctx.market is not None:
            qty = round_amount(qty, ctx.market.precision)
            if (
                qty <= 0
                or qty < ctx.market.min_amount
                or not meets_min_notional(qty, entry, ctx.market.precision)
            ):
                return self._reject(report, "miktar borsa minimumlarının altında")

        # report details
        report.qty = qty
        report.notional = qty * entry
        report.loss_at_stop = abs(entry - stop) * qty
        report.loss_at_stop_pct = report.loss_at_stop / equity * 100 if equity > 0 else ZERO
        fee = ctx.market.taker_fee if ctx.market else Decimal("0.001")
        slip = expected_slippage(ctx.book, side, qty) if ctx.book is not None else None
        report.expected_costs = report.notional * fee * 2 + (slip or ZERO)
        if ctx.candles is not None and len(ctx.candles) > 20:
            a = atr_pct(ctx.candles, self.cfg.analysis.indicators.atr_period).dropna()
            report.atr_pct = float(a.iloc[-1]) if not a.empty else None
            report.realized_vol_30d = realized_vol(ctx.candles["close"], ctx.bars_per_day)
        report.score = classify(report, self.risk.risk_per_trade_pct)
        report.decision = Decision.RESIZE if qty < report.requested_qty else Decision.APPROVE

        intent = OrderIntent(
            exchange=signal.exchange,
            symbol=signal.symbol,
            side=side,
            order_type=OrderType.LIMIT if ctx.ticker is None else OrderType.MARKET,
            amount=qty,
            price=entry if ctx.ticker is None else None,
            stop_loss=stop,
            take_profit=plan.take_profits[-1],
            strategy=signal.strategy,
            reason="; ".join(plan.reasons)[:500],
        )
        return self._approve(intent, report)

    # ---------------------------------------------------------------- exits / external
    def assess_exit(self, intent: OrderIntent, position_qty: Decimal) -> RiskDecision:
        report = RiskReport(
            intent.symbol,
            intent.strategy,
            intent.side.value,
            intent.price or ZERO,
            intent.stop_loss or ZERO,
            intent.amount,
            intent.amount,
        )
        if not intent.reduce_only:
            return self._reject(report, "çıkış emri reduce_only olmalı")
        if intent.amount > position_qty:
            return self._reject(report, "çıkış miktarı pozisyondan büyük")
        report.reasons.append("çıkış emri (kill switch altında da izinli)")
        return self._approve(intent, report)

    def assess_intent(
        self, intent: OrderIntent, portfolio: PortfolioView, ctx: MarketContext
    ) -> RiskDecision:
        """Externally sized intents (e.g. arbitrage legs): gate, kill switch and caps only."""
        report = RiskReport(
            intent.symbol,
            intent.strategy,
            intent.side.value,
            intent.price or ZERO,
            intent.stop_loss or ZERO,
            intent.amount,
            intent.amount,
        )
        if intent.reduce_only:
            return self._reject(report, "reduce_only için assess_exit kullanın")
        if not self.order_gate():
            return self._reject(report, "egress doğrulanmadı (fail-closed)")
        if not self.kill_switch.allows_new_orders():
            return self._reject(report, "kill switch aktif")
        if self.ops is not None and (
            block := self.ops.block_reason(intent.exchange, intent.symbol)
        ):
            return self._reject(report, block)
        px = intent.price or (ctx.ticker.last if ctx.ticker else None)
        if px is None:
            return self._reject(report, "fiyat bilinmiyor")
        notional = intent.amount * px
        eq = self.ops.effective_equity(portfolio.equity) if self.ops else portfolio.equity
        if portfolio.exposure() + notional > eq * self.risk.max_total_exposure_pct / 100:
            return self._reject(report, "toplam maruziyet limiti")
        if (
            portfolio.exposure(intent.symbol) + notional
            > eq * self.risk.max_exposure_per_symbol_pct / 100
        ):
            return self._reject(report, "parite maruziyet limiti")
        report.notional = notional
        return self._approve(intent, report)
