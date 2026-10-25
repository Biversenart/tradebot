"""Every supported exchange: markets, ticker, order book and candles from the PUBLIC API.

No keys, no orders. Run on a machine that can reach the exchanges (ideally through your
egress): pytest -m integration tests/integration/test_all_exchanges_public.py
Some exchanges geo-block certain countries (HTTP 451/403): such a failure means "not usable
from this IP", not a bot bug.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import EgressMode
from bot.config.schema import ExchangeConfig
from bot.exchanges.factory import build_public_adapter
from bot.exchanges.profiles import PROFILES
from bot.marketdata.history import fetch_history
from bot.net.egress import Egress

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", sorted(PROFILES))
async def test_public_market_data(name: str) -> None:
    p = PROFILES[name]
    eg = Egress(mode=EgressMode.DIRECT_VPS, proxy=None, expected_ip=None)
    adapter = build_public_adapter(name, ExchangeConfig(enabled=True), eg, testnet=False)
    async with adapter:
        symbol = f"BTC/{p.quote}"
        assert symbol in adapter.symbols(), f"{symbol} listelenmiyor"
        ticker = await adapter.fetch_ticker(symbol)
        assert ticker.last > 0
        book = await adapter.fetch_order_book(symbol, 20)
        assert book.best_bid is not None and book.best_ask is not None
        candles = await fetch_history(
            adapter, symbol, "1h", datetime.now(UTC) - timedelta(hours=12)
        )
        assert len(candles) >= 6
        info = adapter.market_info(symbol)
        assert info.precision.lot_size > 0 and info.precision.tick_size > 0
