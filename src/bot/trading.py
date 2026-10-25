"""Assembles the live trading stack: storage, risk, execution, strategies (Aşama 5)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from bot.config import Settings
from bot.core.event_bus import EventBus
from bot.core.events import CandleEvent
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import ExchangeAdapterError
from bot.execution.coordinator import CoordinatorConfig, TradingCoordinator
from bot.execution.engine import ExecutionConfig, ExecutionEngine
from bot.execution.reconcile import reconcile
from bot.log import get_logger
from bot.marketdata.feed import MarketDataFeed
from bot.marketdata.history import warm_up
from bot.risk.kill_switch import KillSwitch
from bot.risk.manager import RiskManager
from bot.risk.sizing import GrowthSizer
from bot.storage.repository import Repository
from bot.strategies.base import BaseStrategy
from bot.strategies.registry import build_enabled
from bot.strategies.runner import StrategyRunner

_log = get_logger(__name__)
INITIAL_EQUITY_KEY = "initial_equity"


@dataclass
class TradingStack:
    repo: Repository
    coordinator: TradingCoordinator
    engines: dict[str, ExecutionEngine]
    strategies: list[BaseStrategy] = field(default_factory=list)
    runner: StrategyRunner | None = None

    @classmethod
    async def build(
        cls,
        settings: Settings,
        bus: EventBus,
        exchanges: dict[str, ExchangeAdapter],
        feeds: dict[str, MarketDataFeed],
        order_gate: Callable[[], bool],
    ) -> TradingStack:
        cfg = settings.config
        ex_cfg = cfg.execution
        repo = await Repository.connect(settings.secrets.database_url.get_secret_value())
        kill = KillSwitch(cfg.risk, alert_sink=bus.publish)
        market_type = next(iter(exchanges.values())).market_type if exchanges else "spot"
        sizer = GrowthSizer(cfg.risk, Decimal(1))  # initial equity set in start()
        risk = RiskManager(cfg, kill, sizer, order_gate, market_type)
        exec_cfg = ExecutionConfig(
            order_timeout_seconds=ex_cfg.order_timeout_seconds,
            poll_interval_seconds=ex_cfg.poll_interval_seconds,
            max_retries=ex_cfg.max_retries,
        )
        engines = {
            name: ExecutionEngine(ad, repo, kill, exec_cfg, alert_sink=bus.publish)
            for name, ad in exchanges.items()
        }
        coord = TradingCoordinator(
            cfg,
            bus,
            repo,
            risk,
            engines,
            feeds,
            coord=CoordinatorConfig(
                maintenance_interval_seconds=ex_cfg.maintenance_interval_seconds
            ),
        )
        strategies = [
            s
            for name in exchanges
            for sym in cfg.universe.symbols
            for s in build_enabled(cfg, sym, name)
        ]
        runner = StrategyRunner(bus, strategies) if strategies else None
        return cls(repo, coord, engines, strategies, runner)

    async def start(
        self, settings: Settings, stop: asyncio.Event, tasks: list[asyncio.Task[None]]
    ) -> None:
        coord = self.coordinator
        await coord.restore()
        stored = await self.repo.get_state(INITIAL_EQUITY_KEY)
        equity, _ = await coord.equity()
        initial = Decimal(stored["value"]) if stored else equity
        if not stored and equity > 0:
            await self.repo.set_state(INITIAL_EQUITY_KEY, {"value": str(equity)})
        sizer = coord.sizer
        sizer.initial_equity = initial if initial > 0 else Decimal(1)
        sizer.peak = max(sizer.initial_equity, equity)
        if sizer.lock is not None:
            sizer.lock.level = sizer.initial_equity
        if settings.config.execution.reconcile_on_start:
            for eng in self.engines.values():
                try:
                    rep = await reconcile(eng)
                    _log.info("reconciled", exchange=eng.adapter.name, summary=rep.summary_tr())
                except ExchangeAdapterError as exc:
                    _log.error("reconcile_failed", exchange=eng.adapter.name, error=str(exc))
        await self.warm_up(settings)
        tasks.append(asyncio.create_task(coord.run(stop), name="trading-maintenance"))
        _log.info(
            "trading_started",
            strategies=[f"{s.name}:{s.symbol}" for s in self.strategies],
            equity=str(equity),
        )

    async def warm_up(self, settings: Settings) -> None:
        """Feed recent closed candles to strategies and the coordinator (no signals traded)."""
        bars = settings.config.marketdata.warmup_bars
        seen: set[tuple[str, str, str]] = set()
        for s in self.strategies:
            key = (s.exchange, s.symbol, s.timeframe)
            if key in seen or s.exchange not in self.engines:
                continue
            seen.add(key)
            try:
                candles = await warm_up(
                    self.engines[s.exchange].adapter,
                    s.symbol,
                    s.timeframe,
                    max(bars, s.warmup_bars),
                )
            except ExchangeAdapterError as exc:
                _log.warning("warmup_failed", symbol=s.symbol, error=str(exc))
                continue
            for c in candles:
                for st in self.strategies:
                    st.on_candle(c)
                await self.coordinator.on_candle(CandleEvent(candle=c))

    async def close(self) -> None:
        await self.coordinator.persist()
        await self.repo.close()
