from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from bot.config.schema import AppConfig
from bot.core.clock import ManualClock
from bot.core.event_bus import EventBus
from bot.core.events import AlertLevel, CandleEvent, RiskAlert, SignalEvent
from bot.core.models import (
    Candle,
    Horizon,
    MarketInfo,
    MarketPrecision,
    OrderBook,
    OrderBookLevel,
    PositionSide,
    Signal,
    TradePlan,
)
from bot.exchanges.paper import PaperExchange
from bot.execution.coordinator import TradingCoordinator
from bot.execution.engine import ExecutionConfig, ExecutionEngine
from bot.risk.kill_switch import KillReason, KillSwitch
from bot.risk.manager import RiskManager
from bot.risk.sizing import GrowthSizer
from bot.storage.repository import Repository

T = datetime(2026, 1, 1, tzinfo=UTC)
SYM = "BTC/USDT"
INFO = MarketInfo(
    exchange="paper",
    symbol=SYM,
    base="BTC",
    quote="USDT",
    precision=MarketPrecision(tick_size=D("0.01"), lot_size=D("0.0001"), min_notional=D(5)),
)


def book(bid: str, ask: str) -> OrderBook:
    return OrderBook(
        exchange="paper",
        symbol=SYM,
        timestamp=T,
        bids=(OrderBookLevel(price=D(bid), amount=D(100)),),
        asks=(OrderBookLevel(price=D(ask), amount=D(100)),),
    )


def signal(score: str = "80") -> Signal:
    plan = TradePlan(
        symbol=SYM,
        exchange="paper",
        side=PositionSide.LONG,
        horizon=Horizon.SWING,
        timeframe="1h",
        entry_low=D("99.9"),
        entry_high=D(100),
        stop_loss=D(95),
        take_profits=(D(105), D(110), D(115)),
        confluence_score=D(score),
        invalidation="x",
        created_at=T,
    )
    return Signal(
        strategy="s",
        exchange="paper",
        symbol=SYM,
        timeframe="1h",
        side=PositionSide.LONG,
        score=D(score),
        timestamp=T,
        plan=plan,
    )


def candle(i: int, o: str, h: str, lo: str, c: str) -> CandleEvent:
    return CandleEvent(
        candle=Candle(
            exchange="paper",
            symbol=SYM,
            timeframe="1h",
            open_time=T + timedelta(hours=i),
            open=D(o),
            high=D(h),
            low=D(lo),
            close=D(c),
            volume=D(1),
        )
    )


class World:
    def __init__(self, ex: PaperExchange, repo: Repository) -> None:
        self.ex, self.repo = ex, repo
        self.cfg = AppConfig.model_validate(
            {
                "risk": {"growth": {"profit_lock": {"enabled": False}}},
                "analysis": {"trade_plan": {"trailing": "none", "time_exit_bars": 5}},
            }
        )
        clock = ManualClock(T)
        self.ks = KillSwitch(self.cfg.risk, clock)
        self.sizer = GrowthSizer(self.cfg.risk, D(10_000))
        self.risk = RiskManager(self.cfg, self.ks, self.sizer)
        self.engine = ExecutionEngine(
            ex,
            repo,
            self.ks,
            ExecutionConfig(
                order_timeout_seconds=0.2, poll_interval_seconds=0.01, retry_backoff_seconds=0
            ),
            clock,
        )
        self.bus = EventBus()
        self.coord = TradingCoordinator(
            self.cfg, self.bus, repo, self.risk, {"paper": self.engine}, clock=clock
        )


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    ex = PaperExchange(
        "paper", initial_balances={"USDT": D(10_000)}, markets={SYM: INFO}, clock=ManualClock(T)
    )
    await ex.process_order_book(book("99.9", "100"))
    repo = await Repository.connect(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    yield World(ex, repo)
    await repo.close()


async def test_signal_opens_risk_sized_protected_position(world: World) -> None:
    await world.coord.on_signal(SignalEvent(signal=signal()))
    [pos] = await world.repo.open_positions()
    assert pos.amount == D(20)  # 1% of 10k over a 5-point stop
    assert pos.stop_order_id is not None
    reports = await world.repo.recent_risk_events()
    assert reports == []  # no alerts on the happy path


async def test_tp1_partial_and_breakeven(world: World) -> None:
    await world.coord.on_signal(SignalEvent(signal=signal()))
    await world.ex.process_order_book(book("105", "105.1"))
    await world.coord.on_candle(candle(1, "100", "105.5", "99.8", "105"))
    [pos] = await world.repo.open_positions()
    assert pos.targets_hit == 1 and pos.amount == D(12)  # 40% of 20 sold
    assert pos.stop_loss == D(100)  # break-even
    stop = await world.ex.fetch_order_by_client_id(pos.stop_order_id or "", SYM)
    assert stop.amount == D(12) and stop.stop_price == D(100)


async def test_time_exit(world: World) -> None:
    await world.coord.on_signal(SignalEvent(signal=signal()))
    for i in range(1, 6):
        await world.coord.on_candle(candle(i, "100", "101", "99", "100"))
    assert await world.repo.open_positions() == []
    assert (await world.repo.closed_positions())[0].close_reason == "time"


async def test_kill_switch_blocks_signals_and_closes_positions(world: World) -> None:
    await world.coord.on_signal(SignalEvent(signal=signal()))
    await world.ks.trigger(KillReason.MANUAL, "test")
    await world.coord.on_signal(SignalEvent(signal=signal()))
    assert len(await world.repo.open_positions()) == 1  # second signal rejected
    await world.coord.tick()
    assert await world.repo.open_positions() == []
    assert (await world.repo.closed_positions())[0].close_reason == "kill_switch"
    state = await world.repo.get_state("kill_switch")
    assert state is not None and "manual" in state["reasons"]


async def test_exchange_stop_fill_detected_and_fed_to_sizer(world: World) -> None:
    await world.coord.on_signal(SignalEvent(signal=signal()))
    await world.ex.process_order_book(book("94", "94.1"))
    await world.coord.tick()
    assert await world.repo.open_positions() == []
    assert world.sizer.tracker.loss_streak == 1
    assert (await world.repo.equity_curve())[-1].equity < D(10_000)


async def test_kill_switch_state_restored_after_restart(world: World) -> None:
    await world.ks.trigger(KillReason.MANUAL, "x")
    await world.coord.persist()
    w2 = World(world.ex, Repository(world.repo.engine))
    await w2.coord.restore()
    assert not w2.ks.allows_new_orders()


async def test_alerts_persisted_and_egress_drives_kill_switch(world: World) -> None:
    await world.coord.on_alert(
        RiskAlert(level=AlertLevel.CRITICAL, code="egress_ip_mismatch", message="m")
    )
    assert not world.ks.allows_new_orders()
    await world.coord.on_alert(
        RiskAlert(level=AlertLevel.INFO, code="egress_ip_recovered", message="ok")
    )
    assert world.ks.allows_new_orders()
    assert len(await world.repo.recent_risk_events()) >= 2
