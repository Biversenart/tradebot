"""Chaos harness (spec §5.13): an adapter that injects faults into a PaperExchange."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from bot.config.schema import AppConfig
from bot.core.clock import ManualClock
from bot.core.event_bus import EventBus
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
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.paper import PaperExchange
from bot.execution.coordinator import TradingCoordinator
from bot.execution.engine import ExecutionConfig, ExecutionEngine
from bot.risk.kill_switch import KillSwitch
from bot.risk.manager import RiskManager
from bot.risk.sizing import GrowthSizer
from bot.storage.repository import Repository
from tests.unit.execution.test_coordinator import INFO, SYM, T, book


class ChaosAdapter(ExchangeAdapter):
    """Delegates to `inner`; `inject(method, *errors)` makes the next calls raise.

    A `None` entry lets that call through, so `inject("create_order", None, err)` fails the
    second call only.
    """

    def __init__(self, inner: PaperExchange) -> None:
        self.inner = inner
        self.name = inner.name
        self.market_type = inner.market_type
        self.faults: dict[str, list[BaseException | None]] = {}
        self.calls: dict[str, int] = {}
        self.time_offset = timedelta(0)

    def inject(self, method: str, *errors: BaseException | None) -> None:
        self.faults.setdefault(method, []).extend(errors)

    def _hit(self, method: str) -> None:
        self.calls[method] = self.calls.get(method, 0) + 1
        queue = self.faults.get(method)
        if queue:
            err = queue.pop(0)
            if err is not None:
                raise err

    async def load_markets(self) -> None:
        self._hit("load_markets")
        await self.inner.load_markets()

    async def close(self) -> None:
        await self.inner.close()

    def market_info(self, symbol: str) -> MarketInfo:
        return self.inner.market_info(symbol)

    def symbols(self) -> list[str]:
        return self.inner.symbols()

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: datetime | None = None, limit: int | None = None
    ) -> list[Candle]:
        self._hit("fetch_ohlcv")
        return await self.inner.fetch_ohlcv(symbol, timeframe, since, limit)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        self._hit("fetch_ticker")
        return await self.inner.fetch_ticker(symbol)

    async def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        self._hit("fetch_order_book")
        return await self.inner.fetch_order_book(symbol, depth)

    def watch_ticker(self, symbol: str) -> AsyncIterator[Ticker]:
        return self.inner.watch_ticker(symbol)

    def watch_order_book(self, symbol: str, depth: int = 20) -> AsyncIterator[OrderBook]:
        return self.inner.watch_order_book(symbol, depth)

    def watch_trades(self, symbol: str) -> AsyncIterator[Trade]:
        return self.inner.watch_trades(symbol)

    async def fetch_balance(self) -> dict[str, Balance]:
        self._hit("fetch_balance")
        return await self.inner.fetch_balance()

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        self._hit("fetch_open_orders")
        return await self.inner.fetch_open_orders(symbol)

    async def fetch_my_trades(
        self, symbol: str, since: datetime | None = None, limit: int | None = None
    ) -> list[Fill]:
        return await self.inner.fetch_my_trades(symbol, since, limit)

    async def create_order(self, request: OrderRequest) -> Order:
        self._hit("create_order")
        return await self.inner.create_order(request)

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        self._hit("cancel_order")
        return await self.inner.cancel_order(order_id, symbol)

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        self._hit("fetch_order")
        return await self.inner.fetch_order(order_id, symbol)

    async def fetch_order_by_client_id(self, client_order_id: str, symbol: str) -> Order:
        self._hit("fetch_order_by_client_id")
        return await self.inner.fetch_order_by_client_id(client_order_id, symbol)

    async def server_time_offset(self) -> timedelta:
        self._hit("server_time_offset")
        return self.time_offset


class ChaosWorld:
    def __init__(self, paper: PaperExchange, repo: Repository, cfg: AppConfig) -> None:
        self.paper = paper
        self.ex = ChaosAdapter(paper)
        self.repo = repo
        self.cfg = cfg
        self.clock = ManualClock(T)
        self.ks = KillSwitch(cfg.risk, self.clock)
        self.sizer = GrowthSizer(cfg.risk, D(10_000))
        self.risk = RiskManager(cfg, self.ks, self.sizer)
        self.engine = ExecutionEngine(
            self.ex,
            repo,
            self.ks,
            ExecutionConfig(
                order_timeout_seconds=0.2, poll_interval_seconds=0.01, retry_backoff_seconds=0
            ),
            self.clock,
        )
        self.bus = EventBus()
        self.coord = TradingCoordinator(
            cfg, self.bus, repo, self.risk, {"paper": self.engine}, clock=self.clock
        )


def chaos_config(**risk: object) -> AppConfig:
    return AppConfig.model_validate(
        {
            "risk": {"growth": {"profit_lock": {"enabled": False}}, **risk},
            "analysis": {"trade_plan": {"trailing": "none"}},
        }
    )


@pytest.fixture
async def chaos(tmp_path: Path) -> AsyncIterator[ChaosWorld]:
    paper = PaperExchange(
        "paper", initial_balances={"USDT": D(10_000)}, markets={SYM: INFO}, clock=ManualClock(T)
    )
    await paper.process_order_book(book("99.9", "100"))
    repo = await Repository.connect(f"sqlite+aiosqlite:///{tmp_path / 'chaos.db'}")
    yield ChaosWorld(paper, repo, chaos_config(max_consecutive_errors=3))
    await repo.close()
