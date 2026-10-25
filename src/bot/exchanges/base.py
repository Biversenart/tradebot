"""`ExchangeAdapter`: the only interface the rest of the bot uses to talk to an exchange.

Strategy code never imports ccxt; only modules under `bot.exchanges` do.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from bot.core.models import (
    Balance,
    Candle,
    Fill,
    MarketInfo,
    Order,
    OrderBook,
    OrderRequest,
    Ticker,
    Trade,
)
from bot.exchanges.errors import OrderNotFoundError


class ExchangeAdapter(ABC):
    name: str
    market_type: str = "spot"
    testnet: bool = False

    # ---------------------------------------------------------------- lifecycle
    @abstractmethod
    async def load_markets(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def __aenter__(self) -> ExchangeAdapter:
        await self.load_markets()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ---------------------------------------------------------------- market data
    @abstractmethod
    def market_info(self, symbol: str) -> MarketInfo: ...

    @abstractmethod
    def symbols(self) -> list[str]: ...

    @abstractmethod
    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]: ...

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker: ...

    @abstractmethod
    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook: ...

    @abstractmethod
    def watch_ticker(self, symbol: str) -> AsyncIterator[Ticker]: ...

    @abstractmethod
    def watch_order_book(self, symbol: str, depth: int = 20) -> AsyncIterator[OrderBook]: ...

    @abstractmethod
    def watch_trades(self, symbol: str) -> AsyncIterator[Trade]: ...

    # ---------------------------------------------------------------- account
    @abstractmethod
    async def fetch_balance(self) -> dict[str, Balance]: ...

    @abstractmethod
    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]: ...

    @abstractmethod
    async def fetch_my_trades(
        self, symbol: str, since: datetime | None = None, limit: int | None = None
    ) -> list[Fill]: ...

    # ---------------------------------------------------------------- orders
    @abstractmethod
    async def create_order(self, request: OrderRequest) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> Order: ...

    @abstractmethod
    async def fetch_order(self, order_id: str, symbol: str) -> Order: ...

    async def fetch_order_by_client_id(self, client_order_id: str, symbol: str) -> Order:
        """Look an order up by our idempotent client id (used after timeouts / restarts)."""
        for o in await self.fetch_open_orders(symbol):
            if o.client_order_id == client_order_id:
                return o
        raise OrderNotFoundError(f"{client_order_id} bulunamadı")

    # ---------------------------------------------------------------- clock
    @abstractmethod
    async def server_time_offset(self) -> timedelta:
        """exchange_time - local_time."""
