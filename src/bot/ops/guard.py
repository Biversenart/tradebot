"""OpsGuard: the operational checks RiskManager consults for every NEW-risk order.

Blocks (any -> reject): stablecoin depeg, clock skew on the exchange, delisting / maintenance
announcements for the pair. Sizing: canary capital cap (live) and low-liquidity factor.
Exits are never blocked here (RiskManager.assess_exit does not consult the guard).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from bot.core.models import OrderBook
from bot.ops.announcements import AnnouncementWatcher
from bot.ops.capital import CapitalCap
from bot.ops.clock_skew import ClockSkewMonitor
from bot.ops.liquidity import LowLiquidityMode
from bot.ops.stablecoin import DepegMonitor

ONE = Decimal(1)


class OpsGuard:
    def __init__(
        self,
        capital: CapitalCap | None = None,
        liquidity: LowLiquidityMode | None = None,
        depeg: DepegMonitor | None = None,
        clock_skew: ClockSkewMonitor | None = None,
        announcements: AnnouncementWatcher | None = None,
    ) -> None:
        self.capital = capital
        self.liquidity = liquidity
        self.depeg = depeg
        self.clock_skew = clock_skew
        self.announcements = announcements

    def block_reason(self, exchange: str, symbol: str) -> str | None:
        checks = (
            self.depeg.block_reason() if self.depeg else None,
            self.clock_skew.block_reason(exchange) if self.clock_skew else None,
            self.announcements.block_reason(exchange, symbol) if self.announcements else None,
        )
        return next((c for c in checks if c), None)

    def effective_equity(self, equity: Decimal) -> Decimal:
        return self.capital.effective_equity(equity) if self.capital else equity

    def risk_factor(self, now: datetime, book: OrderBook | None) -> tuple[Decimal, list[str]]:
        return self.liquidity.assess(now, book) if self.liquidity else (ONE, [])
