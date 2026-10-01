"""Test doubles shared by unit tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bot.core.models import (
    Balance,
    Candle,
    Fill,
    MarketInfo,
    MarketPrecision,
    Order,
    OrderBook,
    OrderRequest,
    Ticker,
    Trade,
)
from bot.core.timeframes import timeframe_delta
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import NotSupportedError


def make_candles(
    n: int,
    start: datetime = datetime(2024, 1, 1, tzinfo=UTC),
    timeframe: str = "1h",
    symbol: str = "BTC/USDT",
    exchange: str = "fake",
    base_price: Decimal = Decimal(100),
) -> list[Candle]:
    step = timeframe_delta(timeframe)
    out = []
    for i in range(n):
        p = base_price + i
        out.append(
            Candle(
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                open_time=start + step * i,
                open=p,
                high=p + 2,
                low=p - 1,
                close=p + 1,
                volume=Decimal(10),
            )
        )
    return out


class FakeAdapter(ExchangeAdapter):
    """In-memory adapter: serves `history` for fetch_ohlcv and scripted streams."""

    def __init__(self, name: str = "fake", history: list[Candle] | None = None) -> None:
        self.name = name
        self.history = history or []
        self.ohlcv_calls: list[tuple[datetime | None, int | None]] = []
        self.tickers: list[Ticker] = []
        self.books: list[OrderBook] = []
        self.trades: list[Trade] = []
        self.closed = False
        self.loaded = False

    async def load_markets(self) -> None:
        self.loaded = True

    async def close(self) -> None:
        self.closed = True

    def market_info(self, symbol: str) -> MarketInfo:
        base, quote = symbol.split("/")
        return MarketInfo(
            exchange=self.name,
            symbol=symbol,
            base=base,
            quote=quote,
            precision=MarketPrecision(
                tick_size=Decimal("0.01"), lot_size=Decimal("0.0001"), min_notional=Decimal(5)
            ),
            maker_fee=Decimal("0.001"),
            taker_fee=Decimal("0.001"),
        )

    def symbols(self) -> list[str]:
        return ["BTC/USDT"]

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        self.ohlcv_calls.append((since, limit))
        rows = [c for c in self.history if since is None or c.open_time >= since]
        return rows[: limit or len(rows)]

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return self.tickers[-1]

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        return self.books[-1]

    async def watch_ticker(self, symbol: str) -> AsyncIterator[Ticker]:
        for t in self.tickers:
            yield t

    async def watch_order_book(self, symbol: str, depth: int = 20) -> AsyncIterator[OrderBook]:
        for ob in self.books:
            yield ob

    async def watch_trades(self, symbol: str) -> AsyncIterator[Trade]:
        for t in self.trades:
            yield t

    async def fetch_balance(self) -> dict[str, Balance]:
        return {}

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        return []

    async def fetch_my_trades(
        self, symbol: str, since: datetime | None = None, limit: int | None = None
    ) -> list[Fill]:
        return []

    async def create_order(self, request: OrderRequest) -> Order:
        raise NotSupportedError("fake")

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        raise NotSupportedError("fake")

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        raise NotSupportedError("fake")

    async def server_time_offset(self) -> timedelta:
        return timedelta(0)
