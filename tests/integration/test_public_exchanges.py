"""Real network tests (public endpoints, no keys). Run with: pytest -m integration"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.binance import BinanceAdapter
from bot.exchanges.btcturk import BtcturkAdapter
from bot.marketdata.history import fetch_history

pytestmark = pytest.mark.integration


@pytest.fixture
async def binance() -> AsyncIterator[ExchangeAdapter]:
    a = BinanceAdapter.create(market_type="spot", testnet=False, proxy_options={})
    async with a:
        yield a


async def test_binance_ohlcv_and_book(binance: ExchangeAdapter) -> None:
    since = datetime.now(UTC) - timedelta(hours=30)
    candles = await fetch_history(binance, "BTC/USDT", "1h", since)
    assert len(candles) >= 24
    ob = await binance.fetch_order_book("BTC/USDT", 20)
    assert ob.best_bid is not None and ob.best_ask is not None
    info = binance.market_info("BTC/USDT")
    assert info.precision.tick_size > 0
    off = await binance.server_time_offset()
    assert abs(off.total_seconds()) < 60


async def test_binance_ws_ticker(binance: ExchangeAdapter) -> None:
    t = await anext(binance.watch_ticker("BTC/USDT"))
    assert t.bid <= t.ask


async def test_btcturk_public() -> None:
    a = BtcturkAdapter.create(testnet=True, proxy_options={})
    async with a:
        t = await a.fetch_ticker("BTC/TRY")
        assert t.last > 0
