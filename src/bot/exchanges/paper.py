"""PaperExchange: live market data, simulated orders and balances (spec §5.1).

Fill model (spot):
- Market orders walk the opposite side of the order book, consuming depth level by level
  (realistic slippage). Unfilled remainder is cancelled (IOC). No liquidity -> rejected.
- Marketable limit orders fill immediately as taker up to the limit price; the remainder
  rests as a maker order and fills at its limit price when the book crosses it.
- Stop orders trigger on the best bid (sell stop) / best ask (buy stop) and then execute as
  market (STOP_MARKET) or limit (STOP_LIMIT).
- Fees are charged in the quote currency (taker for immediate fills, maker for resting).
- Balances are reserved for resting orders (free -> used) like a real exchange.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from bot.core.clock import Clock, SystemClock
from bot.core.models import (
    Balance,
    Candle,
    Fill,
    MarketInfo,
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
from bot.core.precision import round_amount, round_price
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import (
    InsufficientFundsError,
    InvalidOrderError,
    NotSupportedError,
    OrderNotFoundError,
)

FillCallback = Callable[[Fill], Awaitable[None]]
ZERO = Decimal(0)


def walk_book(
    levels: Sequence[OrderBookLevel], side: Side, amount: Decimal, limit: Decimal | None = None
) -> list[tuple[Decimal, Decimal]]:
    """Consume `amount` from the opposite-side `levels` (best first). Returns (price, qty)."""
    fills: list[tuple[Decimal, Decimal]] = []
    remaining = amount
    for lvl in levels:
        if remaining <= 0:
            break
        if limit is not None and (
            (side is Side.BUY and lvl.price > limit) or (side is Side.SELL and lvl.price < limit)
        ):
            break
        qty = min(remaining, lvl.amount)
        fills.append((lvl.price, qty))
        remaining -= qty
    return fills


@dataclass
class _PaperOrder:
    request: OrderRequest
    exchange_order_id: str
    created_at: datetime
    status: OrderStatus = OrderStatus.OPEN
    filled: Decimal = ZERO
    cost: Decimal = ZERO  # sum(price * qty)
    reserved_asset: str | None = None
    reserved: Decimal = ZERO
    triggered: bool = False
    updated_at: datetime | None = None
    fills: list[Fill] = field(default_factory=list)

    @property
    def remaining(self) -> Decimal:
        return self.request.amount - self.filled

    @property
    def is_open(self) -> bool:
        return not self.status.is_terminal

    def to_order(self, exchange: str) -> Order:
        r = self.request
        return Order(
            client_order_id=r.client_order_id,
            exchange_order_id=self.exchange_order_id,
            exchange=exchange,
            symbol=r.symbol,
            side=r.side,
            order_type=r.order_type,
            amount=r.amount,
            price=r.price,
            stop_price=r.stop_price,
            status=self.status,
            filled=self.filled,
            average_price=(self.cost / self.filled) if self.filled > 0 else None,
            intent_id=r.intent_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class PaperExchange(ExchangeAdapter):
    def __init__(
        self,
        name: str,
        *,
        data_source: ExchangeAdapter | None = None,
        initial_balances: dict[str, Decimal] | None = None,
        markets: dict[str, MarketInfo] | None = None,
        clock: Clock | None = None,
        on_fill: FillCallback | None = None,
    ) -> None:
        self.name = name
        self.market_type = data_source.market_type if data_source else "spot"
        self.testnet = False
        self._source = data_source
        self._free: dict[str, Decimal] = dict(initial_balances or {})
        self._used: dict[str, Decimal] = {}
        self._markets: dict[str, MarketInfo] = dict(markets or {})
        self._clock = clock or SystemClock()
        self._on_fill = on_fill
        self._orders: dict[str, _PaperOrder] = {}  # by client_order_id
        self._by_exchange_id: dict[str, str] = {}
        self._books: dict[str, OrderBook] = {}
        self._fills: list[Fill] = []
        self._seq = 0
        if self.market_type != "spot":
            raise NotSupportedError("PaperExchange şimdilik yalnızca spot destekler.")

    # ---------------------------------------------------------------- lifecycle
    async def load_markets(self) -> None:
        if self._source is not None:
            await self._source.load_markets()

    async def close(self) -> None:
        if self._source is not None:
            await self._source.close()

    # ---------------------------------------------------------------- market data
    def market_info(self, symbol: str) -> MarketInfo:
        if symbol not in self._markets:
            if self._source is None:
                raise InvalidOrderError(f"{self.name}: bilinmeyen sembol {symbol}")
            self._markets[symbol] = self._source.market_info(symbol).model_copy(
                update={"exchange": self.name}
            )
        return self._markets[symbol]

    def symbols(self) -> list[str]:
        if self._source is not None:
            return self._source.symbols()
        return list(self._markets)

    def _require_source(self) -> ExchangeAdapter:
        if self._source is None:
            raise NotSupportedError("PaperExchange'e veri kaynağı bağlı değil.")
        return self._source

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        return await self._require_source().fetch_ohlcv(symbol, timeframe, since, limit)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return await self._require_source().fetch_ticker(symbol)

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        if self._source is None and symbol in self._books:
            return self._books[symbol]  # externally fed book (no live source)
        ob = await self._require_source().fetch_order_book(symbol, depth)
        await self.process_order_book(ob)
        return ob

    async def watch_ticker(self, symbol: str) -> AsyncIterator[Ticker]:
        async for t in self._require_source().watch_ticker(symbol):
            yield t

    async def watch_order_book(self, symbol: str, depth: int = 20) -> AsyncIterator[OrderBook]:
        async for ob in self._require_source().watch_order_book(symbol, depth):
            await self.process_order_book(ob)
            yield ob

    async def watch_trades(self, symbol: str) -> AsyncIterator[Trade]:
        async for t in self._require_source().watch_trades(symbol):
            yield t

    # ---------------------------------------------------------------- account
    async def fetch_balance(self) -> dict[str, Balance]:
        assets = set(self._free) | set(self._used)
        return {
            a: Balance(asset=a, free=self._free.get(a, ZERO), used=self._used.get(a, ZERO))
            for a in sorted(assets)
            if self._free.get(a, ZERO) > 0 or self._used.get(a, ZERO) > 0
        }

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            o.to_order(self.name)
            for o in self._orders.values()
            if o.is_open and (symbol is None or o.request.symbol == symbol)
        ]

    async def fetch_my_trades(
        self, symbol: str, since: datetime | None = None, limit: int | None = None
    ) -> list[Fill]:
        rows = [
            f for f in self._fills if f.symbol == symbol and (since is None or f.timestamp >= since)
        ]
        return rows[-limit:] if limit else rows

    # ---------------------------------------------------------------- balances
    def _move(self, asset: str, amount: Decimal, *, to_used: bool) -> None:
        src, dst = (self._free, self._used) if to_used else (self._used, self._free)
        if src.get(asset, ZERO) < amount:
            raise InsufficientFundsError(f"Yetersiz {asset} bakiyesi.")
        src[asset] = src.get(asset, ZERO) - amount
        dst[asset] = dst.get(asset, ZERO) + amount

    def _reserve(self, po: _PaperOrder, info: MarketInfo, price: Decimal) -> None:
        r = po.request
        if r.side is Side.BUY:
            asset, amount = info.quote, price * po.remaining * (1 + info.maker_fee)
        else:
            asset, amount = info.base, po.remaining
        self._move(asset, amount, to_used=True)
        po.reserved_asset, po.reserved = asset, amount

    def _release(self, po: _PaperOrder) -> None:
        if po.reserved_asset and po.reserved > 0:
            self._move(po.reserved_asset, po.reserved, to_used=False)
        po.reserved_asset, po.reserved = None, ZERO

    # ---------------------------------------------------------------- order entry
    def _validate(self, request: OrderRequest, info: MarketInfo) -> OrderRequest:
        amount = round_amount(request.amount, info.precision)
        price = (
            round_price(request.price, info.precision, request.side)
            if request.price is not None
            else None
        )
        stop = (
            round_price(request.stop_price, info.precision)
            if request.stop_price is not None
            else None
        )
        if amount <= 0 or amount < info.min_amount:
            raise InvalidOrderError("Miktar lot büyüklüğü / minimum miktarın altında.")
        ref = price or stop
        if ref is not None and amount * ref < info.precision.min_notional:
            raise InvalidOrderError("Emir tutarı min_notional altında.")
        return request.model_copy(update={"amount": amount, "price": price, "stop_price": stop})

    async def _current_book(self, symbol: str) -> OrderBook:
        if self._source is not None:
            return await self.fetch_order_book(symbol)
        if symbol not in self._books:
            raise InvalidOrderError(f"{symbol} için orderbook yok.")
        return self._books[symbol]

    async def create_order(self, request: OrderRequest) -> Order:
        if request.exchange != self.name:
            raise InvalidOrderError(f"Emir {request.exchange} için, borsa {self.name}.")
        existing = self._orders.get(request.client_order_id)
        if existing is not None:  # idempotent client_order_id
            return existing.to_order(self.name)
        info = self.market_info(request.symbol)
        request = self._validate(request, info)
        self._seq += 1
        po = _PaperOrder(request, f"paper-{self._seq}", self._clock.now())

        if request.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
            ref = request.price or request.stop_price
            assert ref is not None  # noqa: S101 - guaranteed by OrderRequest validation
            self._reserve(po, info, ref)
            self._register(po)
            book = self._books.get(request.symbol)
            if book is not None:
                await self._check_stop(po, book, info)
            return po.to_order(self.name)

        book = await self._current_book(request.symbol)
        if request.order_type is OrderType.MARKET:
            await self._execute_taker(po, book, info, limit=None)
            if po.filled == 0:
                po.status = OrderStatus.REJECTED
            elif po.remaining > 0:
                po.status = OrderStatus.CANCELED  # IOC remainder
        else:
            await self._execute_taker(po, book, info, limit=request.price)
            if po.remaining > 0:
                assert request.price is not None  # noqa: S101
                self._reserve(po, info, request.price)
        self._register(po)
        return po.to_order(self.name)

    def _register(self, po: _PaperOrder) -> None:
        self._orders[po.request.client_order_id] = po
        self._by_exchange_id[po.exchange_order_id] = po.request.client_order_id

    async def _execute_taker(
        self, po: _PaperOrder, book: OrderBook, info: MarketInfo, limit: Decimal | None
    ) -> None:
        r = po.request
        levels = book.asks if r.side is Side.BUY else book.bids
        fills = walk_book(levels, r.side, po.remaining, limit)
        if not fills:
            return
        qty = sum((q for _, q in fills), ZERO)
        cost = sum((p * q for p, q in fills), ZERO)
        fee = cost * info.taker_fee
        if r.side is Side.BUY and self._free.get(info.quote, ZERO) < cost + fee:
            raise InsufficientFundsError(f"Yetersiz {info.quote} bakiyesi.")
        if r.side is Side.SELL and self._free.get(info.base, ZERO) < qty:
            raise InsufficientFundsError(f"Yetersiz {info.base} bakiyesi.")
        for price, q in fills:
            await self._apply_fill(po, info, price, q, maker=False, from_reserved=False)

    async def _apply_fill(
        self,
        po: _PaperOrder,
        info: MarketInfo,
        price: Decimal,
        qty: Decimal,
        *,
        maker: bool,
        from_reserved: bool,
    ) -> None:
        r = po.request
        notional = price * qty
        fee = notional * (info.maker_fee if maker else info.taker_fee)
        if r.side is Side.BUY:
            spend = notional + fee
            if from_reserved:
                take = min(spend, po.reserved)
                self._used[info.quote] = self._used.get(info.quote, ZERO) - take
                po.reserved -= take
                if spend > take:
                    self._free[info.quote] = self._free.get(info.quote, ZERO) - (spend - take)
            else:
                self._free[info.quote] = self._free.get(info.quote, ZERO) - spend
            self._free[info.base] = self._free.get(info.base, ZERO) + qty
        else:
            if from_reserved:
                self._used[info.base] = self._used.get(info.base, ZERO) - qty
                po.reserved -= qty
            else:
                self._free[info.base] = self._free.get(info.base, ZERO) - qty
            self._free[info.quote] = self._free.get(info.quote, ZERO) + notional - fee
        po.filled += qty
        po.cost += notional
        now = self._clock.now()
        po.updated_at = now
        po.status = OrderStatus.FILLED if po.remaining <= 0 else OrderStatus.PARTIALLY_FILLED
        if po.status is OrderStatus.FILLED:
            self._release(po)  # leftover reservation (price improvement / fee rounding)
        fill = Fill(
            trade_id=f"{po.exchange_order_id}-{len(po.fills) + 1}",
            client_order_id=r.client_order_id,
            exchange=self.name,
            symbol=r.symbol,
            side=r.side,
            price=price,
            amount=qty,
            fee=fee,
            fee_currency=info.quote,
            is_maker=maker,
            timestamp=now,
        )
        po.fills.append(fill)
        self._fills.append(fill)
        if self._on_fill is not None:
            await self._on_fill(fill)

    # ---------------------------------------------------------------- book-driven matching
    async def process_order_book(self, book: OrderBook) -> None:
        """Match resting orders against a new book snapshot."""
        self._books[book.symbol] = book
        for po in list(self._orders.values()):
            if not po.is_open or po.request.symbol != book.symbol:
                continue
            info = self.market_info(po.request.symbol)
            if (
                po.request.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT)
                and not po.triggered
            ):
                await self._check_stop(po, book, info)
            elif po.request.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
                await self._match_resting(po, book, info)

    async def _check_stop(self, po: _PaperOrder, book: OrderBook, info: MarketInfo) -> None:
        r = po.request
        stop = r.stop_price
        assert stop is not None  # noqa: S101
        bid, ask = book.best_bid, book.best_ask
        hit = (r.side is Side.SELL and bid is not None and bid.price <= stop) or (
            r.side is Side.BUY and ask is not None and ask.price >= stop
        )
        if not hit:
            return
        po.triggered = True
        if r.order_type is OrderType.STOP_MARKET:
            self._release(po)
            try:
                await self._execute_taker(po, book, info, limit=None)
            except InsufficientFundsError:
                po.status = OrderStatus.REJECTED
                return
            if po.filled == 0:
                po.status = OrderStatus.REJECTED
            elif po.remaining > 0:
                po.status = OrderStatus.CANCELED
        else:
            await self._match_resting(po, book, info)

    async def _match_resting(self, po: _PaperOrder, book: OrderBook, info: MarketInfo) -> None:
        r = po.request
        limit = r.price
        assert limit is not None  # noqa: S101
        levels = book.asks if r.side is Side.BUY else book.bids
        available = sum((q for _, q in walk_book(levels, r.side, po.remaining, limit)), ZERO)
        if available > 0:
            await self._apply_fill(po, info, limit, available, maker=True, from_reserved=True)

    # ---------------------------------------------------------------- cancel / query
    def _lookup(self, order_id: str) -> _PaperOrder:
        cid = self._by_exchange_id.get(order_id, order_id)
        po = self._orders.get(cid)
        if po is None:
            raise OrderNotFoundError(f"Emir bulunamadı: {order_id}")
        return po

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        po = self._lookup(order_id)
        if po.is_open:
            self._release(po)
            po.status = OrderStatus.CANCELED
            po.updated_at = self._clock.now()
        return po.to_order(self.name)

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        return self._lookup(order_id).to_order(self.name)

    async def fetch_order_by_client_id(self, client_order_id: str, symbol: str) -> Order:
        return self._lookup(client_order_id).to_order(self.name)

    async def server_time_offset(self) -> timedelta:
        return timedelta(0)
