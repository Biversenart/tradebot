"""Order-book walking helpers shared by the scanners."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from bot.core.models import OrderBookLevel

ZERO = Decimal(0)


def buy_cost(asks: Sequence[OrderBookLevel], qty: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(filled_qty, quote_cost, worst_price) for buying `qty` base by walking asks."""
    remaining, cost, worst = qty, ZERO, ZERO
    for lvl in asks:
        if remaining <= 0:
            break
        take = min(remaining, lvl.amount)
        cost += take * lvl.price
        remaining -= take
        worst = lvl.price
    return qty - remaining, cost, worst


def sell_proceeds(bids: Sequence[OrderBookLevel], qty: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    remaining, proceeds, worst = qty, ZERO, ZERO
    for lvl in bids:
        if remaining <= 0:
            break
        take = min(remaining, lvl.amount)
        proceeds += take * lvl.price
        remaining -= take
        worst = lvl.price
    return qty - remaining, proceeds, worst


def spend_quote(asks: Sequence[OrderBookLevel], quote: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(base_received, quote_spent, worst_price) spending up to `quote` on the asks."""
    remaining, base, worst = quote, ZERO, ZERO
    for lvl in asks:
        if remaining <= 0:
            break
        level_quote = lvl.price * lvl.amount
        take_quote = min(remaining, level_quote)
        base += take_quote / lvl.price
        remaining -= take_quote
        worst = lvl.price
    return base, quote - remaining, worst


def total_depth(levels: Sequence[OrderBookLevel]) -> Decimal:
    return sum((lvl.amount for lvl in levels), ZERO)
