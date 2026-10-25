"""ccxt-backed adapter. This package is the ONLY place allowed to import ccxt.

All numbers coming from ccxt are converted to `Decimal` via `str()`; all ccxt exceptions are
translated to `bot.exchanges.errors`. Proxy settings come exclusively from `Egress`; there is
no code path that drops them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

import ccxt.async_support as ccxt_async
import ccxt.pro as ccxt_pro
from ccxt.base import errors as ccxt_errors
from ccxt.base.decimal_to_precision import DECIMAL_PLACES, SIGNIFICANT_DIGITS

from bot.core.models import (
    Balance,
    Candle,
    Fill,
    MarketInfo,
    MarketPrecision,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Ticker,
    Trade,
)
from bot.core.timeframes import from_ms, timeframe_seconds, to_ms
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import (
    AuthError,
    ExchangeAdapterError,
    ExchangeNetworkError,
    ExchangeUnavailableError,
    InsufficientFundsError,
    InvalidIpError,
    InvalidOrderError,
    NotSupportedError,
    OrderNotFoundError,
    RateLimitError,
    RestrictedLocationError,
)
from bot.log import get_logger, mask_text
from bot.net.errors import ErrorKind, classify_http_error

T = TypeVar("T")
_log = get_logger(__name__)


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _req_decimal(value: Any, what: str) -> Decimal:
    d = to_decimal(value)
    if d is None:
        raise ExchangeAdapterError(f"Borsa yanıtında {what} eksik.")
    return d


def _ts(value: Any) -> datetime:
    return from_ms(value) if value is not None else datetime.now(UTC)


_STATUS = {
    "open": OrderStatus.OPEN,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def translate_error(exc: Exception) -> ExchangeAdapterError:
    """Map a ccxt exception to an adapter error (message masked, kind classified)."""
    msg = mask_text(str(exc))
    if isinstance(exc, ccxt_errors.OrderNotFound):
        return OrderNotFoundError(msg)
    if isinstance(exc, ccxt_errors.InsufficientFunds):
        return InsufficientFundsError(msg)
    if isinstance(exc, ccxt_errors.InvalidOrder):
        return InvalidOrderError(msg)
    if isinstance(exc, ccxt_errors.NotSupported):
        return NotSupportedError(msg)
    if isinstance(exc, ccxt_errors.DDoSProtection | ccxt_errors.RateLimitExceeded):
        if "451" in msg or "restricted location" in msg.lower():
            return RestrictedLocationError(msg)
        return RateLimitError(msg)
    if isinstance(exc, ccxt_errors.AuthenticationError | ccxt_errors.PermissionDenied):
        kind = classify_http_error(401, msg)
        return InvalidIpError(msg) if kind is ErrorKind.INVALID_IP else AuthError(msg)
    if isinstance(exc, ccxt_errors.ExchangeNotAvailable | ccxt_errors.OnMaintenance):
        if "451" in msg or "restricted location" in msg.lower():
            return RestrictedLocationError(msg)
        return ExchangeUnavailableError(msg)
    if isinstance(exc, ccxt_errors.NetworkError):
        return ExchangeNetworkError(msg)
    if isinstance(exc, ccxt_errors.ExchangeError):
        kind = classify_http_error(400, msg)
        if kind is ErrorKind.INVALID_IP:
            return InvalidIpError(msg)
        if kind is ErrorKind.RESTRICTED_LOCATION:
            return RestrictedLocationError(msg)
        return ExchangeAdapterError(msg, kind)
    return ExchangeAdapterError(msg)


class CcxtAdapter(ExchangeAdapter):
    """Generic ccxt adapter; concrete exchanges subclass it to configure the client."""

    reconnect_initial_delay = 1.0
    reconnect_max_delay = 30.0
    poll_interval = 2.0

    def __init__(
        self,
        name: str,
        client: Any,
        *,
        market_type: str = "spot",
        testnet: bool = False,
        supports_ws: bool = True,
    ) -> None:
        self.name = name
        self._client = client
        self.market_type = market_type
        self.testnet = testnet
        self._supports_ws = supports_ws
        self._markets_loaded = False

    # ---------------------------------------------------------------- client factory
    @staticmethod
    def build_client(
        exchange_id: str,
        *,
        proxy_options: dict[str, Any],
        api_key: str | None = None,
        secret: str | None = None,
        options: dict[str, Any] | None = None,
        pro: bool = True,
    ) -> Any:
        module: Any = ccxt_pro if pro and hasattr(ccxt_pro, exchange_id) else ccxt_async
        config: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {"adjustForTimeDifference": True, **(options or {})},
            **proxy_options,
        }
        if api_key and secret:
            config["apiKey"] = api_key
            config["secret"] = secret
        client = getattr(module, exchange_id)(config)
        client.aiohttp_trust_env = False  # never pick up HTTP(S)_PROXY from env
        return client

    # ---------------------------------------------------------------- helpers
    async def _call(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
        except ccxt_errors.BaseError as exc:
            raise translate_error(exc) from exc

    async def load_markets(self) -> None:
        await self._call(self._client.load_markets)
        self._markets_loaded = True

    async def close(self) -> None:
        await self._client.close()

    def symbols(self) -> list[str]:
        return list(self._client.symbols or [])

    def _market(self, symbol: str) -> dict[str, Any]:
        markets = self._client.markets or {}
        if symbol not in markets:
            raise InvalidOrderError(f"{self.name}: bilinmeyen sembol {symbol}")
        market: dict[str, Any] = markets[symbol]
        return market

    def _step(self, value: Any) -> Decimal:
        """Convert a ccxt precision value to a step size."""
        mode = getattr(self._client, "precisionMode", None)
        d = to_decimal(value)
        if d is None:
            return Decimal("1e-8")
        if mode in (DECIMAL_PLACES, SIGNIFICANT_DIGITS):
            return Decimal(1).scaleb(-int(d))
        return d

    def market_info(self, symbol: str) -> MarketInfo:
        m = self._market(symbol)
        precision = m.get("precision") or {}
        limits = m.get("limits") or {}
        return MarketInfo(
            exchange=self.name,
            symbol=symbol,
            base=m["base"],
            quote=m["quote"],
            market_type=self.market_type,
            precision=MarketPrecision(
                tick_size=self._step(precision.get("price")),
                lot_size=self._step(precision.get("amount")),
                min_notional=to_decimal((limits.get("cost") or {}).get("min")) or Decimal(0),
            ),
            min_amount=to_decimal((limits.get("amount") or {}).get("min")) or Decimal(0),
            maker_fee=to_decimal(m.get("maker")) or Decimal("0.001"),
            taker_fee=to_decimal(m.get("taker")) or Decimal("0.001"),
        )

    # ---------------------------------------------------------------- converters
    def _candle(self, symbol: str, timeframe: str, row: list[Any], closed: bool) -> Candle:
        ts, o, h, low, c, v = row[:6]
        return Candle(
            exchange=self.name,
            symbol=symbol,
            timeframe=timeframe,
            open_time=from_ms(ts),
            open=_req_decimal(o, "open"),
            high=_req_decimal(h, "high"),
            low=_req_decimal(low, "low"),
            close=_req_decimal(c, "close"),
            volume=to_decimal(v) or Decimal(0),
            closed=closed,
        )

    def _ticker(self, symbol: str, t: dict[str, Any]) -> Ticker:
        last = to_decimal(t.get("last")) or to_decimal(t.get("close"))
        bid = to_decimal(t.get("bid")) or last
        ask = to_decimal(t.get("ask")) or last
        return Ticker(
            exchange=self.name,
            symbol=symbol,
            timestamp=_ts(t.get("timestamp")),
            bid=_req_decimal(bid, "bid"),
            ask=_req_decimal(ask, "ask"),
            last=_req_decimal(last, "last"),
            bid_volume=to_decimal(t.get("bidVolume")),
            ask_volume=to_decimal(t.get("askVolume")),
        )

    def _order_book(self, symbol: str, ob: dict[str, Any], depth: int) -> OrderBook:
        def levels(rows: list[list[Any]]) -> tuple[OrderBookLevel, ...]:
            out = []
            for row in rows[:depth]:
                price, amount = to_decimal(row[0]), to_decimal(row[1])
                if price and amount and price > 0 and amount > 0:
                    out.append(OrderBookLevel(price=price, amount=amount))
            return tuple(out)

        return OrderBook(
            exchange=self.name,
            symbol=symbol,
            timestamp=_ts(ob.get("timestamp")),
            bids=levels(ob.get("bids") or []),
            asks=levels(ob.get("asks") or []),
        )

    def _trade(self, symbol: str, t: dict[str, Any]) -> Trade:
        side = t.get("side")
        return Trade(
            exchange=self.name,
            symbol=symbol,
            trade_id=str(t.get("id") or t.get("timestamp")),
            price=_req_decimal(t.get("price"), "price"),
            amount=_req_decimal(t.get("amount"), "amount"),
            side=Side(side) if side in ("buy", "sell") else None,
            timestamp=_ts(t.get("timestamp")),
        )

    def _order(self, o: dict[str, Any]) -> Order:
        raw_type = (o.get("type") or "market").lower()
        stop = to_decimal(o.get("triggerPrice") or o.get("stopPrice") or o.get("stopLossPrice"))
        if "stop" in raw_type or stop is not None:
            order_type = OrderType.STOP_LIMIT if "limit" in raw_type else OrderType.STOP_MARKET
        else:
            order_type = OrderType.LIMIT if raw_type == "limit" else OrderType.MARKET
        amount = _req_decimal(o.get("amount"), "amount")
        filled = to_decimal(o.get("filled")) or Decimal(0)
        status = _STATUS.get(o.get("status") or "open", OrderStatus.OPEN)
        if status is OrderStatus.OPEN and 0 < filled < amount:
            status = OrderStatus.PARTIALLY_FILLED
        price = to_decimal(o.get("price"))
        avg = to_decimal(o.get("average"))
        return Order(
            client_order_id=str(o.get("clientOrderId") or o.get("id")),
            exchange_order_id=str(o["id"]) if o.get("id") is not None else None,
            exchange=self.name,
            symbol=o["symbol"],
            side=Side(o["side"]),
            order_type=order_type,
            amount=amount,
            price=price if price and price > 0 else None,
            stop_price=stop if stop and stop > 0 else None,
            status=status,
            filled=min(filled, amount),
            average_price=avg if avg and avg > 0 else None,
            created_at=_ts(o.get("timestamp")),
            updated_at=_ts(o.get("lastUpdateTimestamp")) if o.get("lastUpdateTimestamp") else None,
        )

    def _fill(self, t: dict[str, Any]) -> Fill:
        fee = t.get("fee") or {}
        return Fill(
            trade_id=str(t["id"]),
            client_order_id=str(t.get("clientOrderId") or t.get("order") or ""),
            exchange=self.name,
            symbol=t["symbol"],
            side=Side(t["side"]),
            price=_req_decimal(t.get("price"), "price"),
            amount=_req_decimal(t.get("amount"), "amount"),
            fee=to_decimal(fee.get("cost")) or Decimal(0),
            fee_currency=fee.get("currency"),
            is_maker=t.get("takerOrMaker") == "maker",
            timestamp=_ts(t.get("timestamp")),
        )

    # ---------------------------------------------------------------- REST market data
    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        rows: list[list[Any]] = await self._call(
            self._client.fetch_ohlcv,
            symbol,
            timeframe,
            to_ms(since) if since else None,
            limit,
        )
        now_ms = to_ms(datetime.now(UTC))
        tf_ms = timeframe_seconds(timeframe) * 1000
        return [self._candle(symbol, timeframe, r, closed=r[0] + tf_ms <= now_ms) for r in rows]

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return self._ticker(symbol, await self._call(self._client.fetch_ticker, symbol))

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        ob = await self._call(self._client.fetch_order_book, symbol, depth)
        return self._order_book(symbol, ob, depth)

    # ---------------------------------------------------------------- streaming
    async def _stream(
        self, what: str, fetch_once: Callable[[], Awaitable[T]], has_ws: bool
    ) -> AsyncIterator[T]:
        """Yield items forever; reconnect with backoff on network errors (same egress)."""
        delay = self.reconnect_initial_delay
        while True:
            try:
                item = await fetch_once()
                delay = self.reconnect_initial_delay
                yield item
                if not has_ws:
                    await asyncio.sleep(self.poll_interval)
            except (ExchangeNetworkError, ExchangeUnavailableError, RateLimitError) as exc:
                _log.warning(
                    "stream_reconnect", exchange=self.name, stream=what, error=str(exc), delay=delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.reconnect_max_delay)

    def _has_ws(self, capability: str) -> bool:
        has = getattr(self._client, "has", {}) or {}
        return self._supports_ws and bool(has.get(capability))

    async def watch_ticker(self, symbol: str) -> AsyncIterator[Ticker]:
        if self._has_ws("watchTicker"):

            async def once() -> Ticker:
                return self._ticker(symbol, await self._call(self._client.watch_ticker, symbol))

            async for t in self._stream("ticker", once, True):
                yield t
        else:
            async for t in self._stream("ticker", lambda: self.fetch_ticker(symbol), False):
                yield t

    async def watch_order_book(self, symbol: str, depth: int = 20) -> AsyncIterator[OrderBook]:
        if self._has_ws("watchOrderBook"):

            async def once() -> OrderBook:
                ob = await self._call(self._client.watch_order_book, symbol, depth)
                return self._order_book(symbol, ob, depth)

            async for ob in self._stream("order_book", once, True):
                yield ob
        else:
            stream = self._stream("order_book", lambda: self.fetch_order_book(symbol, depth), False)
            async for ob in stream:
                yield ob

    async def watch_trades(self, symbol: str) -> AsyncIterator[Trade]:
        seen: set[str] = set()
        if self._has_ws("watchTrades"):

            async def once() -> list[dict[str, Any]]:
                rows: list[dict[str, Any]] = await self._call(self._client.watch_trades, symbol)
                return rows

            stream = self._stream("trades", once, True)
        else:

            async def poll() -> list[dict[str, Any]]:
                rows: list[dict[str, Any]] = await self._call(self._client.fetch_trades, symbol)
                return rows

            stream = self._stream("trades", poll, False)
        async for batch in stream:
            for row in batch:
                trade = self._trade(symbol, row)
                if trade.trade_id in seen:
                    continue
                seen.add(trade.trade_id)
                if len(seen) > 10_000:
                    seen.clear()
                yield trade

    # ---------------------------------------------------------------- account
    async def fetch_balance(self) -> dict[str, Balance]:
        raw = await self._call(self._client.fetch_balance)
        free: dict[str, Any] = raw.get("free") or {}
        used: dict[str, Any] = raw.get("used") or {}
        out: dict[str, Balance] = {}
        for asset in set(free) | set(used):
            f = to_decimal(free.get(asset)) or Decimal(0)
            u = to_decimal(used.get(asset)) or Decimal(0)
            if f > 0 or u > 0:
                out[asset] = Balance(asset=asset, free=f, used=u)
        return out

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        rows = await self._call(self._client.fetch_open_orders, symbol)
        return [self._order(o) for o in rows]

    async def fetch_my_trades(
        self, symbol: str, since: datetime | None = None, limit: int | None = None
    ) -> list[Fill]:
        rows = await self._call(
            self._client.fetch_my_trades, symbol, to_ms(since) if since else None, limit
        )
        return [self._fill(t) for t in rows]

    # ---------------------------------------------------------------- orders
    def order_params(self, request: OrderRequest) -> tuple[str, dict[str, Any]]:
        """ccxt order type + params for a request (stop orders become stop-loss orders)."""
        params: dict[str, Any] = {"clientOrderId": request.client_order_id}
        if request.reduce_only:
            params["reduceOnly"] = True
        if request.order_type is OrderType.MARKET:
            return "market", params
        if request.order_type is OrderType.LIMIT:
            return "limit", params
        params["stopLossPrice"] = float(request.stop_price or 0)
        return ("limit" if request.order_type is OrderType.STOP_LIMIT else "market"), params

    async def create_order(self, request: OrderRequest) -> Order:
        if request.exchange != self.name:
            raise InvalidOrderError(f"Emir {request.exchange} için, adapter {self.name}.")
        ccxt_type, params = self.order_params(request)
        raw = await self._call(
            self._client.create_order,
            request.symbol,
            ccxt_type,
            request.side.value,
            float(request.amount),
            float(request.price) if request.price is not None else None,
            params,
        )
        raw.setdefault("symbol", request.symbol)
        raw.setdefault("side", request.side.value)
        raw.setdefault("amount", str(request.amount))
        raw["clientOrderId"] = raw.get("clientOrderId") or request.client_order_id
        order = self._order(raw)
        return order.model_copy(update={"intent_id": request.intent_id})

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        raw = await self._call(self._client.cancel_order, order_id, symbol)
        if not raw.get("amount") or not raw.get("side"):
            return await self.fetch_order(order_id, symbol)
        raw.setdefault("symbol", symbol)
        return self._order(raw)

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        return self._order(await self._call(self._client.fetch_order, order_id, symbol))

    # ---------------------------------------------------------------- clock
    async def server_time_offset(self) -> timedelta:
        before = datetime.now(UTC)
        server_ms = await self._call(self._client.fetch_time)
        after = datetime.now(UTC)
        local_mid = before + (after - before) / 2
        return from_ms(server_ms) - local_mid
