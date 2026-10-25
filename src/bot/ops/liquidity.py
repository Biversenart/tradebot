"""Weekend / holiday / thin-book mode (spec §5.13)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from bot.config.schema import LowLiquidityConfig
from bot.core.models import OrderBook, Side
from bot.risk.report import depth_within

ONE = Decimal(1)
ZERO = Decimal(0)


class LowLiquidityMode:
    def __init__(self, cfg: LowLiquidityConfig) -> None:
        self.cfg = cfg

    def assess(self, now: datetime, book: OrderBook | None = None) -> tuple[Decimal, list[str]]:
        """(risk factor, reasons). Factor 0 means: open no new trades."""
        if not self.cfg.enabled:
            return ONE, []
        factor, notes = ONE, []
        if now.weekday() >= 5:
            factor = min(factor, self.cfg.weekend_risk_factor)
            notes.append("hafta sonu")
        if now.date() in self.cfg.holidays:
            factor = min(factor, self.cfg.holiday_risk_factor)
            notes.append("tatil günü")
        if self.cfg.min_depth_quote > 0 and book is not None:
            depth = min(depth_within(book, Side.BUY), depth_within(book, Side.SELL))
            if depth < self.cfg.min_depth_quote:
                factor = min(factor, self.cfg.thin_book_risk_factor)
                notes.append(f"ince orderbook ({depth:.0f} < {self.cfg.min_depth_quote})")
        if notes and self.cfg.action == "halt":
            return ZERO, notes
        return factor, notes
