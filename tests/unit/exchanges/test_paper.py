from __future__ import annotations

from datetime import datetime
from decimal import Decimal as D

import pytest

from bot.core.clock import ManualClock
from bot.core.models import (
    Fill,
    MarketInfo,
    MarketPrecision,
    OrderBook,
    OrderBookLevel,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from bot.exchanges.errors import InsufficientFundsError, InvalidOrderError, OrderNotFoundError
from bot.exchanges.paper import PaperExchange, walk_book

SYM = "BTC/USDT"
INFO = MarketInfo(
    exchange="paper",
    symbol=SYM,
    base="BTC",
    quote="USDT",
    precision=MarketPrecision(tick_size=D("0.01"), lot_size=D("0.001"), min_notional=D("5")),
    maker_fee=D("0.001"),
    taker_fee=D("0.002"),
)


def lv(p: str, a: str) -> OrderBookLevel:
    return OrderBookLevel(price=D(p), amount=D(a))


def book(now: datetime, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> OrderBook:
    return OrderBook(
        exchange="paper",
        symbol=SYM,
        timestamp=now,
        bids=tuple(lv(*b) for b in bids),
        asks=tuple(lv(*a) for a in asks),
    )


def default_book(now: datetime) -> OrderBook:
    return book(now, [("99", "1"), ("98", "2")], [("100", "1"), ("101", "2"), ("102", "5")])


async def make(
    now: datetime, usdt: str = "10000", btc: str = "0"
) -> tuple[PaperExchange, list[Fill]]:
    fills: list[Fill] = []

    async def on_fill(f: Fill) -> None:
        fills.append(f)

    ex = PaperExchange(
        "paper",
        initial_balances={"USDT": D(usdt), "BTC": D(btc)},
        markets={SYM: INFO},
        clock=ManualClock(now),
        on_fill=on_fill,
    )
    await ex.process_order_book(default_book(now))
    return ex, fills


def req(
    cid: str,
    side: Side,
    otype: OrderType,
    amount: str,
    price: str | None = None,
    stop: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_order_id=cid,
        exchange="paper",
        symbol=SYM,
        side=side,
        order_type=otype,
        amount=D(amount),
        price=D(price) if price else None,
        stop_price=D(stop) if stop else None,
    )


def test_walk_book() -> None:
    asks = (lv("100", "1"), lv("101", "2"))
    assert walk_book(asks, Side.BUY, D("2")) == [(D("100"), D("1")), (D("101"), D("1"))]
    assert walk_book(asks, Side.BUY, D("5"), limit=D("100")) == [(D("100"), D("1"))]


async def test_market_buy_walks_depth_with_slippage_and_fees(now: datetime) -> None:
    ex, fills = await make(now)
    o = await ex.create_order(req("m1", Side.BUY, OrderType.MARKET, "2.5"))
    assert o.status is OrderStatus.FILLED
    assert o.filled == D("2.5")
    # 1@100 + 1.5@101 = 251.5 ; avg 100.6
    assert o.average_price == D("100.6")
    fee = D("251.5") * D("0.002")
    bal = await ex.fetch_balance()
    assert bal["BTC"].free == D("2.5")
    assert bal["USDT"].free == D("10000") - D("251.5") - fee
    assert [f.price for f in fills] == [D("100"), D("101")]
    assert all(not f.is_maker for f in fills)


async def test_market_order_insufficient_depth_is_ioc(now: datetime) -> None:
    ex, _ = await make(now, usdt="100000")
    o = await ex.create_order(req("m2", Side.BUY, OrderType.MARKET, "10"))
    assert o.status is OrderStatus.CANCELED
    assert o.filled == D("8")


async def test_market_order_insufficient_funds(now: datetime) -> None:
    ex, fills = await make(now, usdt="50")
    with pytest.raises(InsufficientFundsError):
        await ex.create_order(req("m3", Side.BUY, OrderType.MARKET, "1"))
    assert fills == []
    assert (await ex.fetch_balance())["USDT"].free == D("50")


async def test_sell_requires_base(now: datetime) -> None:
    ex, _ = await make(now)
    with pytest.raises(InsufficientFundsError):
        await ex.create_order(req("s1", Side.SELL, OrderType.MARKET, "0.5"))


async def test_limit_rests_reserves_and_fills_on_cross(now: datetime) -> None:
    ex, fills = await make(now)
    o = await ex.create_order(req("l1", Side.BUY, OrderType.LIMIT, "1", "98.5"))
    assert o.status is OrderStatus.OPEN
    bal = await ex.fetch_balance()
    reserved = D("98.5") * (1 + INFO.maker_fee)
    assert bal["USDT"].used == reserved
    assert len(await ex.fetch_open_orders(SYM)) == 1
    # market drops: ask 98.4 crosses our 98.5 bid
    await ex.process_order_book(book(now, [("98", "1")], [("98.4", "0.4"), ("98.5", "5")]))
    o = await ex.fetch_order("l1", SYM)
    assert o.status is OrderStatus.FILLED
    assert o.average_price == D("98.5")  # maker fills at limit price
    assert all(f.is_maker for f in fills)
    bal = await ex.fetch_balance()
    assert "USDT" not in bal or bal["USDT"].used == 0
    assert bal["BTC"].free == D("1")
    assert bal["USDT"].free == D("10000") - D("98.5") * (1 + INFO.maker_fee)


async def test_marketable_limit_partial_then_rests(now: datetime) -> None:
    ex, _ = await make(now)
    o = await ex.create_order(req("l2", Side.BUY, OrderType.LIMIT, "2", "100"))
    assert o.status is OrderStatus.PARTIALLY_FILLED
    assert o.filled == D("1")
    assert (await ex.fetch_balance())["USDT"].used == D("100") * (1 + INFO.maker_fee)


async def test_cancel_releases_reservation(now: datetime) -> None:
    ex, _ = await make(now)
    await ex.create_order(req("l3", Side.BUY, OrderType.LIMIT, "1", "90"))
    o = await ex.cancel_order("l3", SYM)
    assert o.status is OrderStatus.CANCELED
    bal = await ex.fetch_balance()
    assert bal["USDT"].free == D("10000") and bal["USDT"].used == 0
    with pytest.raises(OrderNotFoundError):
        await ex.cancel_order("nope", SYM)


async def test_stop_market_triggers_on_bid(now: datetime) -> None:
    ex, fills = await make(now, btc="1")
    o = await ex.create_order(req("sl", Side.SELL, OrderType.STOP_MARKET, "1", stop="97"))
    assert o.status is OrderStatus.OPEN
    assert (await ex.fetch_balance())["BTC"].used == D("1")
    await ex.process_order_book(book(now, [("97.5", "5")], [("98", "5")]))
    assert (await ex.fetch_order("sl", SYM)).status is OrderStatus.OPEN
    await ex.process_order_book(book(now, [("96.9", "0.6"), ("96", "5")], [("97", "5")]))
    o = await ex.fetch_order("sl", SYM)
    assert o.status is OrderStatus.FILLED
    assert [f.price for f in fills] == [D("96.9"), D("96")]  # gap slippage through stop


async def test_stop_limit(now: datetime) -> None:
    ex, _ = await make(now, btc="1")
    await ex.create_order(req("slx", Side.SELL, OrderType.STOP_LIMIT, "1", price="96.5", stop="97"))
    await ex.process_order_book(book(now, [("96.9", "0.5")], [("97", "5")]))
    o = await ex.fetch_order("slx", SYM)
    assert o.status is OrderStatus.PARTIALLY_FILLED and o.filled == D("0.5")
    assert o.average_price == D("96.5")


async def test_idempotent_client_order_id(now: datetime) -> None:
    ex, fills = await make(now)
    a = await ex.create_order(req("dup", Side.BUY, OrderType.MARKET, "0.5"))
    b = await ex.create_order(req("dup", Side.BUY, OrderType.MARKET, "0.5"))
    assert a == b
    assert len(fills) == 1


async def test_precision_and_min_notional(now: datetime) -> None:
    ex, _ = await make(now)
    o = await ex.create_order(req("p1", Side.BUY, OrderType.MARKET, "0.12345"))
    assert o.amount == D("0.123")
    with pytest.raises(InvalidOrderError):
        await ex.create_order(req("p2", Side.BUY, OrderType.LIMIT, "0.01", "100"))  # 1 USDT < 5
    with pytest.raises(InvalidOrderError):
        await ex.create_order(req("p3", Side.BUY, OrderType.MARKET, "0.0004"))


async def test_fetch_my_trades(now: datetime) -> None:
    ex, _ = await make(now)
    await ex.create_order(req("t1", Side.BUY, OrderType.MARKET, "1.5"))
    trades = await ex.fetch_my_trades(SYM)
    assert len(trades) == 2
    assert len(await ex.fetch_my_trades(SYM, limit=1)) == 1


async def test_wrong_exchange_rejected(now: datetime) -> None:
    ex, _ = await make(now)
    r = req("w", Side.BUY, OrderType.MARKET, "1").model_copy(update={"exchange": "binance"})
    with pytest.raises(InvalidOrderError):
        await ex.create_order(r)
