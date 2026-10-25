"""Chaos: thin books -> partial fills on entry and exit."""

from __future__ import annotations

from decimal import Decimal as D

from bot.core.events import SignalEvent
from bot.core.models import OrderBook, OrderBookLevel, OrderStatus
from tests.chaos.conftest import ChaosWorld
from tests.unit.execution.test_coordinator import SYM, T, candle, signal


def thin(bid: str, bid_qty: str, ask: str, ask_qty: str) -> OrderBook:
    return OrderBook(
        exchange="paper",
        symbol=SYM,
        timestamp=T,
        bids=(OrderBookLevel(price=D(bid), amount=D(bid_qty)),),
        asks=(OrderBookLevel(price=D(ask), amount=D(ask_qty)),),
    )


async def stop_order(chaos: ChaosWorld, pid: str) -> D:
    pos = await chaos.repo.get_position(pid)
    assert pos is not None and pos.stop_order_id is not None
    o = await chaos.paper.fetch_order_by_client_id(pos.stop_order_id, SYM)
    assert o.status is OrderStatus.OPEN
    return o.amount


async def test_partial_entry_protects_only_filled_size(chaos: ChaosWorld) -> None:
    await chaos.paper.process_order_book(thin("99.9", "100", "100", "5"))
    await chaos.coord.on_signal(SignalEvent(signal=signal()))  # wants 20, book has 5
    [pos] = await chaos.repo.open_positions()
    assert pos.amount == D(5) and pos.initial_amount == D(5)
    assert await stop_order(chaos, pos.position_id) == D(5)
    # the unfilled remainder was cancelled at the timeout: nothing rests on the book
    resting = [o for o in await chaos.paper.fetch_open_orders(SYM) if o.side.value == "buy"]
    assert resting == []
    bal = await chaos.paper.fetch_balance()
    assert bal["USDT"].used == 0 and bal["BTC"].total == D(5)


async def test_partial_exit_keeps_stop_in_sync(chaos: ChaosWorld) -> None:
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    [pos] = await chaos.repo.open_positions()
    assert pos.amount == D(20)
    # TP1 (105) touched, but only 3 BTC of bids: 40 % (8) cannot fully fill
    await chaos.paper.process_order_book(thin("105", "3", "105.1", "100"))
    await chaos.coord.on_candle(candle(1, "100", "106", "100", "105"))
    fresh = await chaos.repo.get_position(pos.position_id)
    assert fresh is not None and fresh.status == "open"
    assert D(12) <= fresh.amount < D(20)  # reduced by what actually filled
    assert await stop_order(chaos, pos.position_id) == fresh.amount  # stop = remaining size
    bal = await chaos.paper.fetch_balance()
    assert bal["BTC"].total == fresh.amount
