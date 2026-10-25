"""Rounding to exchange precision (tick size, lot size, min notional)."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

from bot.core.models import MarketPrecision, Side


def _quantize_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        raise ValueError("step pozitif olmalı")
    units = (value / step).to_integral_value(rounding=rounding)
    return (units * step).quantize(step)


def round_amount(amount: Decimal, precision: MarketPrecision) -> Decimal:
    """Round amount DOWN to lot size (never exceed intended size)."""
    return _quantize_to_step(amount, precision.lot_size, ROUND_DOWN)


def round_price(price: Decimal, precision: MarketPrecision, side: Side | None = None) -> Decimal:
    """Round price to tick size.

    For limit orders round conservatively: BUY down, SELL up. Without a side, round half-up.
    """
    if side is Side.BUY:
        rounding = ROUND_DOWN
    elif side is Side.SELL:
        rounding = ROUND_UP
    else:
        rounding = ROUND_HALF_UP
    return _quantize_to_step(price, precision.tick_size, rounding)


def meets_min_notional(amount: Decimal, price: Decimal, precision: MarketPrecision) -> bool:
    return amount * price >= precision.min_notional
