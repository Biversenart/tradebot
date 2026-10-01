"""Live market data feed: exchange streams -> EventBus events (+ candles built from trades)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from bot.core.aio import wait_or_stop
from bot.core.clock import Clock, SystemClock
from bot.core.event_bus import EventBus
from bot.core.events import CandleEvent, OrderBookEvent, TickerEvent
from bot.core.models import OrderBook, Ticker
from bot.exchanges.base import ExchangeAdapter
from bot.log import get_logger
from bot.marketdata.candles import MultiTimeframeBuilder

_log = get_logger(__name__)


class MarketDataFeed:
    def __init__(
        self,
        adapter: ExchangeAdapter,
        bus: EventBus,
        symbols: Iterable[str],
        timeframes: Iterable[str] = ("1m", "5m", "15m", "1h", "4h", "1d"),
        *,
        depth: int = 20,
        clock: Clock | None = None,
    ) -> None:
        self.adapter = adapter
        self.bus = bus
        self.symbols = list(symbols)
        self.depth = depth
        self._clock = clock or SystemClock()
        self.builders = {
            s: MultiTimeframeBuilder(adapter.name, s, list(timeframes)) for s in self.symbols
        }
        self.tickers: dict[str, Ticker] = {}
        self.books: dict[str, OrderBook] = {}

    async def _tickers(self, symbol: str) -> None:
        async for t in self.adapter.watch_ticker(symbol):
            self.tickers[symbol] = t
            await self.bus.publish(TickerEvent(ticker=t))

    async def _books(self, symbol: str) -> None:
        async for ob in self.adapter.watch_order_book(symbol, self.depth):
            self.books[symbol] = ob
            await self.bus.publish(OrderBookEvent(order_book=ob))

    async def _trades(self, symbol: str) -> None:
        builder = self.builders[symbol]
        async for trade in self.adapter.watch_trades(symbol):
            for candle in builder.add_trade(trade):
                await self.bus.publish(CandleEvent(candle=candle))

    async def _flusher(self, stop: asyncio.Event) -> None:
        while not await wait_or_stop(stop, 1.0):
            now = self._clock.now()
            for builder in self.builders.values():
                for candle in builder.flush(now):
                    await self.bus.publish(CandleEvent(candle=candle))

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._flusher(stop))]
        for s in self.symbols:
            tasks += [
                asyncio.create_task(self._tickers(s), name=f"ticker:{s}"),
                asyncio.create_task(self._books(s), name=f"book:{s}"),
                asyncio.create_task(self._trades(s), name=f"trades:{s}"),
            ]
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                    _log.error("feed_task_failed", error=str(r))
