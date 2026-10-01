from __future__ import annotations

from decimal import Decimal as D

import pytest

from bot.core.models import MarketPrecision, Side
from bot.core.precision import meets_min_notional, round_amount, round_price

P = MarketPrecision(tick_size=D("0.01"), lot_size=D("0.001"), min_notional=D("10"))


def test_round_amount_down() -> None:
    assert round_amount(D("0.12399"), P) == D("0.123")
    assert round_amount(D("0.0009"), P) == D("0.000")


def test_round_price_by_side() -> None:
    assert round_price(D("100.017"), P, Side.BUY) == D("100.01")
    assert round_price(D("100.011"), P, Side.SELL) == D("100.02")
    assert round_price(D("100.015"), P) == D("100.02")
    assert round_price(D("100.01"), P, Side.SELL) == D("100.01")


def test_non_decimal_step_sizes() -> None:
    p = MarketPrecision(tick_size=D("0.5"), lot_size=D("5"))
    assert round_price(D("10.74"), p, Side.BUY) == D("10.5")
    assert round_amount(D("23"), p) == D("20")


def test_min_notional() -> None:
    assert meets_min_notional(D("0.1"), D("100"), P)
    assert not meets_min_notional(D("0.099"), D("100"), P)


def test_invalid_precision() -> None:
    with pytest.raises(ValueError):
        MarketPrecision(tick_size=D("0"), lot_size=D("1"))
