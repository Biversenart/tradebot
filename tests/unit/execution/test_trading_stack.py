from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from pydantic import SecretStr

from bot.config import Mode, Settings
from bot.config.schema import AppConfig
from bot.config.secrets import Secrets
from bot.core.event_bus import EventBus
from bot.core.events import SignalEvent
from bot.core.models import OrderBook, OrderBookLevel
from bot.exchanges.paper import PaperExchange
from bot.marketdata.feed import MarketDataFeed
from bot.trading import TradingStack
from tests.fakes import FakeAdapter, make_candles
from tests.unit.execution.test_coordinator import signal


async def test_stack_reconciles_warms_up_and_trades(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    hist = make_candles(400, start=now - timedelta(hours=400), exchange="paper")
    source = FakeAdapter("paper", history=hist)
    last = float(hist[-1].close)
    source.books = [
        OrderBook(
            exchange="paper",
            symbol="BTC/USDT",
            timestamp=now,
            bids=(OrderBookLevel(price=D(str(last - 0.01)), amount=D(100)),),
            asks=(OrderBookLevel(price=D(str(last)), amount=D(100)),),
        )
    ]
    ex = PaperExchange("paper", data_source=source, initial_balances={"USDT": D(10_000)})
    cfg = AppConfig.model_validate(
        {
            "universe": {"symbols": ["BTC/USDT"]},
            "strategies": {"breakout": {"enabled": True}},
            "marketdata": {"warmup_bars": 300},
            "risk": {"growth": {"profit_lock": {"enabled": False}}},
            "execution": {"order_timeout_seconds": 0.2, "poll_interval_seconds": 0.01},
        }
    )
    secrets = Secrets(
        _env_file=None, database_url=SecretStr(f"sqlite+aiosqlite:///{tmp_path / 'live.db'}")
    )
    settings = Settings(cfg, secrets, Mode.PAPER)
    bus = EventBus()
    feed = MarketDataFeed(ex, bus, ["BTC/USDT"], ["1h"])
    stack = await TradingStack.build(settings, bus, {"paper": ex}, {"paper": feed}, lambda: True)
    assert [s.name for s in stack.strategies] == ["breakout"]
    stop = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    bus_task = asyncio.create_task(bus.run())
    await stack.start(settings, stop, tasks)
    assert len(stack.strategies[0]._rows) >= 300  # warmed up
    assert stack.coordinator.frame("paper", "BTC/USDT", "1h") is not None
    assert (await stack.repo.get_state("initial_equity")) == {"value": "10000"}
    sig = signal().model_copy(update={"exchange": "paper"})
    plan = sig.plan
    assert plan is not None
    sig = sig.model_copy(
        update={
            "plan": plan.model_copy(
                update={
                    "exchange": "paper",
                    "entry_low": D(str(last - 1)),
                    "entry_high": D(str(last)),
                    "stop_loss": D(str(last - 5)),
                    "take_profits": (D(str(last + 10)),),
                }
            )
        }
    )
    await bus.publish(SignalEvent(signal=sig))
    for _ in range(100):
        await asyncio.sleep(0.01)
        opened = await stack.repo.open_positions()
        if opened and opened[0].stop_order_id:
            break
    [pos] = await stack.repo.open_positions()
    assert pos.stop_order_id is not None
    stop.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await bus.stop()
    await bus_task
    await stack.close()


async def test_arbitrage_wiring_paper_executes_testnet_logs_only(tmp_path: Path) -> None:
    exchanges = {n: PaperExchange(n, initial_balances={"USDT": D(100)}) for n in ("a", "b")}
    cfg = AppConfig.model_validate(
        {
            "arbitrage": {"cross_exchange": {"enabled": True, "pairs": ["BTC/USDT"]}},
        }
    )
    for mode, has_exec in ((Mode.PAPER, True), (Mode.TESTNET, False)):
        secrets = Secrets(
            _env_file=None, database_url=SecretStr(f"sqlite+aiosqlite:///{tmp_path / f'{mode}.db'}")
        )
        stack = await TradingStack.build(
            Settings(cfg, secrets, mode), EventBus(), exchanges, {}, lambda: True
        )
        assert stack.arbitrage is not None
        assert (stack.arbitrage.executor is not None) is has_exec
        await stack.close()
