"""Chaos: websocket disconnects, broken streams and stale data."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal as D

from bot.core.clock import ManualClock
from bot.core.event_bus import EventBus
from bot.core.events import PositionEvent, SignalEvent
from bot.core.models import Ticker
from bot.exchanges.ccxt_adapter import CcxtAdapter
from bot.marketdata.feed import MarketDataFeed
from tests.chaos.conftest import ChaosWorld
from tests.unit.exchanges.test_ccxt_adapter import FakeClient
from tests.unit.execution.test_coordinator import SYM, T, book, signal


async def test_flapping_websocket_reconnects_with_capped_backoff() -> None:
    client = FakeClient()
    a = CcxtAdapter("binance", client)
    a.reconnect_initial_delay = 0.001
    a.reconnect_max_delay = 0.004
    got: list[Ticker] = []
    stream = a.watch_ticker("BTC/USDT")
    for burst in (5, 0, 3):  # repeated disconnect bursts
        client.watch_failures = burst
        got.append(await asyncio.wait_for(anext(stream), 2))
    assert len(got) == 3 and client.watch_failures == 0


async def test_ws_dead_while_price_crashes_exchange_stop_still_protects(
    chaos: ChaosWorld,
) -> None:
    events: list[PositionEvent] = []

    async def spy(e: PositionEvent) -> None:
        events.append(e)

    chaos.bus.subscribe(PositionEvent, spy)
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    [pos] = await chaos.repo.open_positions()
    # the bot receives nothing (WS down) while the market falls through the stop: the stop
    # lives on the exchange (rule 9) and executes there
    await chaos.paper.process_order_book(book("90", "90.1"))
    # REST maintenance after reconnect notices the fill and books the loss
    await chaos.coord.tick()
    assert await chaos.repo.open_positions() == []
    closed = await chaos.repo.get_position(pos.position_id)
    assert closed is not None and closed.close_reason == "stop"
    assert closed.realized_pnl < 0


class Broken:
    """Adapter stub whose ticker stream dies with an unexpected error, then works."""

    name = "broken"

    def __init__(self) -> None:
        self.attempts = 0

    async def _gen(self) -> AsyncIterator[Ticker]:
        self.attempts += 1
        if self.attempts <= 2:
            raise ValueError("unparseable frame")
        yield Ticker(exchange="broken", symbol=SYM, bid=D(99), ask=D(100), last=D(100), timestamp=T)
        await asyncio.Event().wait()

    def watch_ticker(self, symbol: str) -> AsyncIterator[Ticker]:
        return self._gen()

    async def _idle(self) -> AsyncIterator[Ticker]:
        await asyncio.Event().wait()
        yield  # type: ignore[misc]  # pragma: no cover

    def watch_order_book(self, symbol: str, depth: int = 20) -> AsyncIterator[Ticker]:
        return self._idle()

    def watch_trades(self, symbol: str) -> AsyncIterator[Ticker]:
        return self._idle()


async def test_feed_supervisor_restarts_dead_stream_and_flags_stale_data() -> None:
    clock = ManualClock(T)
    ad = Broken()
    feed = MarketDataFeed(ad, EventBus(), [SYM], ["1m"], clock=clock)  # type: ignore[arg-type]
    feed.restart_initial_delay = 0.001
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if feed.fresh_ticker(SYM) is not None:
            break
    assert ad.attempts == 3 and feed.restarts[f"ticker:{SYM}"] == 2
    assert feed.fresh_ticker(SYM) is not None
    clock.advance(timedelta(seconds=31))  # no updates for > stale_after
    assert feed.fresh_ticker(SYM) is None and feed.tickers[SYM].last == D(100)
    stop.set()
    await task


async def test_stale_ticker_never_prices_an_order(chaos: ChaosWorld) -> None:
    feed = MarketDataFeed(chaos.ex, chaos.bus, [SYM], ["1h"], clock=chaos.clock)
    feed.tickers[SYM] = Ticker(
        exchange="paper", symbol=SYM, bid=D(49), ask=D(50), last=D(50), timestamp=T
    )
    feed.ticker_seen[SYM] = T - timedelta(minutes=10)  # WS silent for 10 minutes
    chaos.coord.feeds = {"paper": feed}
    assert chaos.coord.mark("paper", SYM) is None  # stale ticker ignored
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    [pos] = await chaos.repo.open_positions()
    assert pos.entry_price == D(100)  # limit at the plan price, not the stale 50
