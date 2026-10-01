"""CcxtAdapter conversions, order params and error translation (offline, fake ccxt client)."""

from __future__ import annotations

import asyncio
from decimal import Decimal as D
from typing import Any

import ccxt
import pytest
from ccxt.base.decimal_to_precision import DECIMAL_PLACES, TICK_SIZE

from bot.core.models import OrderRequest, OrderStatus, OrderType, Side
from bot.exchanges import errors as e
from bot.exchanges.ccxt_adapter import CcxtAdapter, translate_error
from bot.net.errors import ErrorKind

T0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z


class FakeClient:
    def __init__(self, precision_mode: int = TICK_SIZE) -> None:
        self.precisionMode = precision_mode
        self.markets: dict[str, Any] = {
            "BTC/USDT": {
                "base": "BTC",
                "quote": "USDT",
                "precision": {"price": 0.01, "amount": 0.00001}
                if precision_mode == TICK_SIZE
                else {"price": 2, "amount": 5},
                "limits": {"cost": {"min": 5.0}, "amount": {"min": 0.00001}},
                "maker": 0.001,
                "taker": 0.001,
            }
        }
        self.symbols = list(self.markets)
        self.has: dict[str, bool] = {
            "watchTicker": True,
            "watchOrderBook": True,
            "watchTrades": True,
        }
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.watch_failures = 0
        self.closed = False

    async def load_markets(self) -> dict[str, Any]:
        return self.markets

    async def close(self) -> None:
        self.closed = True

    async def fetch_ohlcv(self, *a: Any) -> list[list[Any]]:
        self.calls.append(("fetch_ohlcv", a))
        return [[T0, 100.1, 110.0, 95.5, 105.25, 12.5], [T0 + 3_600_000, 105.25, 106, 104, 105, 1]]

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"bid": 99.9, "ask": 100.1, "last": 100.0, "timestamp": T0, "bidVolume": 1.5}

    async def watch_ticker(self, symbol: str) -> dict[str, Any]:
        if self.watch_failures > 0:
            self.watch_failures -= 1
            raise ccxt.NetworkError("socket closed")
        return await self.fetch_ticker(symbol)

    async def fetch_order_book(self, symbol: str, limit: int) -> dict[str, Any]:
        return {
            "bids": [[99.9, 1.0], [99.8, 2.0], [99.7, 0.0]],
            "asks": [[100.1, 0.5], [100.2, 3.0]],
            "timestamp": None,
        }

    async def fetch_trades(self, symbol: str) -> list[dict[str, Any]]:
        return [{"id": "1", "price": 100.0, "amount": 0.1, "side": "buy", "timestamp": T0}]

    async def fetch_balance(self) -> dict[str, Any]:
        return {
            "free": {"USDT": 1000.5, "BTC": 0.0, "ETH": None},
            "used": {"USDT": 10.0, "BTC": 0.0},
        }

    async def create_order(self, *a: Any) -> dict[str, Any]:
        self.calls.append(("create_order", a))
        symbol, typ, side, amount, price, params = a
        return {
            "id": "123",
            "clientOrderId": params.get("clientOrderId"),
            "symbol": symbol,
            "type": typ,
            "side": side,
            "amount": amount,
            "price": price,
            "filled": 0.0,
            "status": "open",
            "timestamp": T0,
            "triggerPrice": params.get("stopLossPrice"),
        }

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return {
            "id": order_id,
            "clientOrderId": "c1",
            "symbol": symbol,
            "type": "limit",
            "side": "buy",
            "amount": 2.0,
            "filled": 0.5,
            "price": 100.0,
            "average": 99.95,
            "status": "open",
            "timestamp": T0,
        }

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return {"id": order_id, "symbol": symbol, "status": "canceled"}

    async def fetch_time(self) -> int:
        import time

        return int(time.time() * 1000) + 1500


def adapter(client: FakeClient | None = None, **kw: Any) -> CcxtAdapter:
    return CcxtAdapter("binance", client or FakeClient(), **kw)


def test_market_info_tick_size_mode() -> None:
    info = adapter().market_info("BTC/USDT")
    assert info.precision.tick_size == D("0.01")
    assert info.precision.lot_size == D("0.00001")
    assert info.precision.min_notional == D("5.0")
    assert (info.base, info.quote) == ("BTC", "USDT")


def test_market_info_decimal_places_mode() -> None:
    info = adapter(FakeClient(DECIMAL_PLACES)).market_info("BTC/USDT")
    assert info.precision.tick_size == D("0.01")
    assert info.precision.lot_size == D("0.00001")


def test_unknown_symbol() -> None:
    with pytest.raises(e.InvalidOrderError):
        adapter().market_info("NOPE/USDT")


async def test_fetch_ohlcv_decimal_and_closed_flag() -> None:
    candles = await adapter().fetch_ohlcv("BTC/USDT", "1h")
    assert candles[0].open == D("100.1") and candles[0].close == D("105.25")
    assert all(c.closed for c in candles)  # 2024 candles are closed
    assert candles[0].timeframe == "1h"


async def test_ticker_and_book() -> None:
    a = adapter()
    t = await a.fetch_ticker("BTC/USDT")
    assert (t.bid, t.ask, t.last, t.bid_volume) == (D("99.9"), D("100.1"), D("100.0"), D("1.5"))
    ob = await a.fetch_order_book("BTC/USDT", depth=2)
    assert len(ob.bids) == 2 and ob.bids[0].price == D("99.9")
    assert ob.best_ask is not None and ob.best_ask.price == D("100.1")


async def test_zero_amount_levels_dropped() -> None:
    ob = await adapter().fetch_order_book("BTC/USDT", depth=20)
    assert [lvl.price for lvl in ob.bids] == [D("99.9"), D("99.8")]


async def test_balance_skips_zero() -> None:
    bal = await adapter().fetch_balance()
    assert set(bal) == {"USDT"}
    assert bal["USDT"].total == D("1010.5")


@pytest.mark.parametrize(
    ("otype", "price", "stop", "ccxt_type", "has_stop"),
    [
        (OrderType.MARKET, None, None, "market", False),
        (OrderType.LIMIT, D("100"), None, "limit", False),
        (OrderType.STOP_MARKET, None, D("95"), "market", True),
        (OrderType.STOP_LIMIT, D("94.5"), D("95"), "limit", True),
    ],
)
async def test_create_order_params(
    otype: OrderType, price: D | None, stop: D | None, ccxt_type: str, has_stop: bool
) -> None:
    client = FakeClient()
    req = OrderRequest(
        client_order_id="abc123",
        exchange="binance",
        symbol="BTC/USDT",
        side=Side.SELL,
        order_type=otype,
        amount=D("0.5"),
        price=price,
        stop_price=stop,
        intent_id="i1",
    )
    order = await adapter(client).create_order(req)
    name, args = client.calls[-1]
    assert name == "create_order"
    assert args[1] == ccxt_type
    assert args[5]["clientOrderId"] == "abc123"
    assert ("stopLossPrice" in args[5]) is has_stop
    assert order.client_order_id == "abc123"
    assert order.intent_id == "i1"
    assert order.order_type is otype


async def test_create_order_rejects_other_exchange() -> None:
    req = OrderRequest(
        client_order_id="x",
        exchange="btcturk",
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        amount=D(1),
    )
    with pytest.raises(e.InvalidOrderError):
        await adapter().create_order(req)


async def test_partial_order_status_and_cancel() -> None:
    a = adapter()
    o = await a.fetch_order("9", "BTC/USDT")
    assert o.status is OrderStatus.PARTIALLY_FILLED
    assert o.remaining == D("1.5")
    assert o.average_price == D("99.95")
    c = await a.cancel_order("9", "BTC/USDT")  # sparse cancel response -> refetch
    assert c.exchange_order_id == "9"


async def test_server_time_offset() -> None:
    off = await adapter().server_time_offset()
    assert 1.0 < off.total_seconds() < 2.0


async def test_watch_ticker_reconnects_on_network_error() -> None:
    client = FakeClient()
    client.watch_failures = 2
    a = adapter(client)
    a.reconnect_initial_delay = 0
    a.reconnect_max_delay = 0
    t = await asyncio.wait_for(anext(a.watch_ticker("BTC/USDT")), 1)
    assert t.last == D("100.0")
    assert client.watch_failures == 0


async def test_polling_fallback_without_ws() -> None:
    client = FakeClient()
    client.has = {}
    a = adapter(client, supports_ws=False)
    a.poll_interval = 0
    gen = a.watch_trades("BTC/USDT")
    first = await asyncio.wait_for(anext(gen), 1)
    assert first.trade_id == "1"
    # duplicate trades from later polls are not re-emitted
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(gen), 0.05)


@pytest.mark.parametrize(
    ("exc", "cls", "kind"),
    [
        (ccxt.OrderNotFound("x"), e.OrderNotFoundError, ErrorKind.OTHER),
        (ccxt.InsufficientFunds("x"), e.InsufficientFundsError, ErrorKind.OTHER),
        (ccxt.InvalidOrder("x"), e.InvalidOrderError, ErrorKind.OTHER),
        (ccxt.RateLimitExceeded("x"), e.RateLimitError, ErrorKind.RATE_LIMIT),
        (
            ccxt.PermissionDenied(
                'binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions"}'
            ),
            e.InvalidIpError,
            ErrorKind.INVALID_IP,
        ),
        (ccxt.AuthenticationError("bad signature"), e.AuthError, ErrorKind.AUTH),
        (
            ccxt.ExchangeNotAvailable("451 Service unavailable from a restricted location"),
            e.RestrictedLocationError,
            ErrorKind.RESTRICTED_LOCATION,
        ),
        (
            ccxt.ExchangeNotAvailable("502 bad gateway"),
            e.ExchangeUnavailableError,
            ErrorKind.SERVER,
        ),
        (ccxt.RequestTimeout("timeout"), e.ExchangeNetworkError, ErrorKind.NETWORK),
        (ccxt.ExchangeError('{"code":-2015}'), e.InvalidIpError, ErrorKind.INVALID_IP),
    ],
)
def test_translate_error(
    exc: Exception, cls: type[e.ExchangeAdapterError], kind: ErrorKind
) -> None:
    out = translate_error(exc)
    assert type(out) is cls
    assert out.kind is kind


def test_translate_error_masks_proxy_credentials() -> None:
    out = translate_error(ccxt.NetworkError("cannot connect socks5://u:hunter2@1.2.3.4:1080"))
    assert "hunter2" not in str(out)


async def test_ccxt_exceptions_never_leak() -> None:
    class Boom(FakeClient):
        async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
            raise ccxt.ExchangeNotAvailable("down")

    with pytest.raises(e.ExchangeUnavailableError):
        await adapter(Boom()).fetch_ticker("BTC/USDT")
