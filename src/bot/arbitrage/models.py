"""Arbitrage opportunity models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from bot.core.models import Side


class ArbKind(StrEnum):
    CROSS = "cross"
    TRIANGULAR = "triangular"


@dataclass(frozen=True)
class Leg:
    exchange: str
    symbol: str
    side: Side
    amount: Decimal  # base amount
    avg_price: Decimal  # expected average fill price after walking the book
    worst_price: Decimal  # deepest level touched (limit price for IOC)


@dataclass(frozen=True)
class Opportunity:
    kind: ArbKind
    route: str
    legs: tuple[Leg, ...]
    gross_pct: Decimal  # before fees / buffers
    net_pct: Decimal  # after fees and slippage buffer
    notional_quote: Decimal  # capital committed (start asset / quote)
    expected_profit_quote: Decimal
    depth_limited: bool
    latency_ms: float
    detected_at: datetime
    notes: list[str] = field(default_factory=list)

    @property
    def summary_tr(self) -> str:
        return (
            f"{self.kind.value} {self.route}: net %{self.net_pct:.3f} "
            f"(brüt %{self.gross_pct:.3f}), "
            f"tutar {self.notional_quote:.2f}, beklenen kâr {self.expected_profit_quote:.4f}"
        )
