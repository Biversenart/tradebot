"""Live market data feed: exchange streams -> EventBus events (+ candles built from trades).

Each stream runs under a supervisor: whatever ends or breaks it (unexpected error, iterator
exhausted) it is restarted with capped exponential backoff, so one bad message never leaves a
symbol silently stale. Consumers that price orders use `fresh_ticker` / `fresh_book`, which
return None once the last update is older than `stale_after` (spec §5.13 chaos tests).
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta

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
    restart_initial_delay = 1.0
    restart_max_delay = 30.0
    stale_after = timedelta(seconds=30)

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
        self.ticker_seen: dict[str, datetime] = {}
        self.book_seen: dict[str, datetime] = {}
        self.restarts: dict[str, int] = {}

    def _fresh(self, seen: dict[str, datetime], symbol: str) -> bool:
        t = seen.get(symbol)
        return t is not None and self._clock.now() - t <= self.stale_after

    def fresh_ticker(self, symbol: str) -> Ticker | None:
        return self.tickers.get(symbol) if self._fresh(self.ticker_seen, symbol) else None

    def fresh_book(self, symbol: str) -> OrderBook | None:
        return self.books.get(symbol) if self._fresh(self.book_seen, symbol) else None

    async def _tickers(self, symbol: str) -> None:
        async for t in self.adapter.watch_ticker(symbol):
            self.tickers[symbol] = t
            self.ticker_seen[symbol] = self._clock.now()
            await self.bus.publish(TickerEvent(ticker=t))

    async def _books(self, symbol: str) -> None:
        async for ob in self.adapter.watch_order_book(symbol, self.depth):
            self.books[symbol] = ob
            self.book_seen[symbol] = self._clock.now()
            await self.bus.publish(OrderBookEvent(order_book=ob))

    async def _supervise(self, name: str, stream: Callable[[], Awaitable[None]]) -> None:
        """Run `stream` forever: restart after errors or an exhausted iterator (same egress)."""
        delay = self.restart_initial_delay
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await stream()
                _log.warning("feed_stream_ended", exchange=self.adapter.name, stream=name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning(
                    "feed_stream_failed",
                    exchange=self.adapter.name,
                    stream=name,
                    error=str(exc),
                    delay=delay,
                )
            self.restarts[name] = self.restarts.get(name, 0) + 1
            if loop.time() - started > 60:
                delay = self.restart_initial_delay
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.restart_max_delay)

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
            streams: tuple[tuple[str, Callable[[str], Awaitable[None]]], ...] = (
                ("ticker", self._tickers),
                ("book", self._books),
                ("trades", self._trades),
            )
            for kind, fn in streams:
                tasks.append(
                    asyncio.create_task(
                        self._supervise(f"{kind}:{s}", functools.partial(fn, s)),
                        name=f"{kind}:{s}",
                    )
                )
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                    _log.error("feed_task_failed", error=str(r))
