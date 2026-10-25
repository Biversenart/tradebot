"""Pre-trade risk report (spec §5.6 "Risk analizi (işlem öncesi)")."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd

from bot.core.models import OrderBook, Side
from bot.exchanges.paper import walk_book


class RiskScore(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def label_tr(self) -> str:
        return {"low": "düşük", "medium": "orta", "high": "yüksek"}[self.value]


class Decision(StrEnum):
    APPROVE = "approve"
    RESIZE = "resize"
    REJECT = "reject"


@dataclass
class RiskReport:
    symbol: str
    strategy: str
    side: str
    entry: Decimal
    stop: Decimal
    qty: Decimal = Decimal(0)
    requested_qty: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    loss_at_stop: Decimal = Decimal(0)
    loss_at_stop_pct: Decimal = Decimal(0)
    expected_costs: Decimal = Decimal(0)
    atr_pct: float | None = None
    realized_vol_30d: float | None = None
    liquidity_score: float | None = None
    max_correlation: float | None = None
    correlated_with: list[str] = field(default_factory=list)
    event: str | None = None
    risk_pct: Decimal = Decimal(0)
    score: RiskScore = RiskScore.LOW
    decision: Decision = Decision.APPROVE
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = str(v)
        d["score"] = self.score.value
        d["decision"] = self.decision.value
        return d

    def summary_tr(self) -> str:
        parts = [
            f"{self.symbol} {self.side.upper()} {self.qty} @ {self.entry} (stop {self.stop})",
            f"stopta zarar {self.loss_at_stop:.2f} (%{self.loss_at_stop_pct:.2f})",
            f"risk: {self.score.label_tr}",
            f"karar: {self.decision.value}",
        ]
        if self.reasons:
            parts.append("; ".join(self.reasons))
        return " · ".join(parts)


def realized_vol(closes: pd.Series, bars_per_day: float, days: int = 30) -> float | None:
    n = int(bars_per_day * days)
    rets = closes.pct_change().dropna().iloc[-n:]
    if len(rets) < 10:
        return None
    return float(rets.std(ddof=0) * np.sqrt(bars_per_day * 365) * 100)


def depth_within(book: OrderBook, side: Side, pct: Decimal = Decimal("0.5")) -> Decimal:
    """Quote-currency depth available within `pct` % of the touch on the taking side."""
    levels = book.asks if side is Side.BUY else book.bids
    if not levels:
        return Decimal(0)
    touch = levels[0].price
    limit = touch * (1 + pct / 100) if side is Side.BUY else touch * (1 - pct / 100)
    total = Decimal(0)
    for lvl in levels:
        if (side is Side.BUY and lvl.price > limit) or (side is Side.SELL and lvl.price < limit):
            break
        total += lvl.price * lvl.amount
    return total


def expected_slippage(book: OrderBook, side: Side, qty: Decimal) -> Decimal | None:
    """Average fill price deviation from the touch (quote amount) for a market order."""
    levels = book.asks if side is Side.BUY else book.bids
    if not levels:
        return None
    fills = walk_book(levels, side, qty)
    filled = sum((q for _, q in fills), Decimal(0))
    if filled <= 0:
        return None
    cost = sum((p * q for p, q in fills), Decimal(0))
    touch = levels[0].price
    return abs(cost - touch * filled)


def classify(report: RiskReport, base_risk_pct: Decimal) -> RiskScore:
    points = 0
    if report.loss_at_stop_pct > base_risk_pct * Decimal("1.25"):
        points += 2
    if report.atr_pct is not None and report.atr_pct > 3:
        points += 1
    if report.realized_vol_30d is not None and report.realized_vol_30d > 120:
        points += 1
    if report.liquidity_score is not None and report.liquidity_score < 0.5:
        points += 2
    if report.max_correlation is not None and report.max_correlation > 0.8:
        points += 1
    if report.event:
        points += 1
    if points >= 3:
        return RiskScore.HIGH
    if points >= 1:
        return RiskScore.MEDIUM
    return RiskScore.LOW
