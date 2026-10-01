from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from bot.core.event_bus import EventBus
from bot.core.events import CandleEvent, Event, OrderBookEvent, TickerEvent
from bot.core.models import OrderBook, OrderBookLevel, Side, Ticker, Trade
from bot.marketdata.feed import MarketDataFeed
from tests.fakes import FakeAdapter

T0 = datetime(2024, 1, 1, tzinfo=UTC)


async def test_feed_publishes_events() -> None:
    ad = FakeAdapter()
    ad.tickers = [
        Ticker(exchange="fake", symbol="BTC/USDT", timestamp=T0, bid=D(99), ask=D(101), last=D(100))
    ]
    ad.books = [
        OrderBook(
            exchange="fake",
            symbol="BTC/USDT",
            timestamp=T0,
            bids=(OrderBookLevel(price=D(99), amount=D(1)),),
            asks=(OrderBookLevel(price=D(101), amount=D(1)),),
        )
    ]
    ad.trades = [
        Trade(
            exchange="fake",
            symbol="BTC/USDT",
            trade_id=str(i),
            price=D(100 + i),
            amount=D(1),
            side=Side.BUY,
            timestamp=T0 + timedelta(seconds=30 * i),
        )
        for i in range(5)
    ]
    bus = EventBus()
    seen: list[Event] = []

    async def collect(e: Event) -> None:
        seen.append(e)

    bus.subscribe(Event, collect)
    bus_task = asyncio.create_task(bus.run())
    feed = MarketDataFeed(ad, bus, ["BTC/USDT"], timeframes=["1m"])
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2)
    await bus.stop()
    await bus_task

    assert any(isinstance(e, TickerEvent) for e in seen)
    assert any(isinstance(e, OrderBookEvent) for e in seen)
    candles = [e.candle for e in seen if isinstance(e, CandleEvent)]
    assert [c.open_time for c in candles] == [T0, T0 + timedelta(minutes=1)]
    assert candles[0].close == D(101)
    assert feed.tickers["BTC/USDT"].last == D(100)
