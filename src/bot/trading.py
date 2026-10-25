"""Assembles the live trading stack: storage, risk, execution, strategies (Aşama 5)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from bot.arbitrage.executor import ArbitrageExecutor
from bot.arbitrage.service import ArbitrageService
from bot.config import Mode, Settings
from bot.core.aio import wait_or_stop
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
from bot.ops import capital as capital_mod
from bot.ops import shadow as shadow_mod
from bot.ops.announcements import HttpGet
from bot.ops.config_log import ConfigChangeLog
from bot.ops.service import OpsService
from bot.ops.shadow import ShadowBook, ShadowPlan, ShadowRegistry, ShadowRunner
from bot.risk.kill_switch import KillSwitch
from bot.risk.manager import RiskManager
from bot.risk.portfolio import PortfolioView
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
    arbitrage: ArbitrageService | None = None
    ops: OpsService | None = None
    shadow_registry: ShadowRegistry = field(default_factory=ShadowRegistry)
    shadow_plan: ShadowPlan | None = None
    shadow_book: ShadowBook = field(default_factory=ShadowBook)
    shadow_runner: ShadowRunner | None = None
    shadow_strategies: list[BaseStrategy] = field(default_factory=list)
    config_log: ConfigChangeLog | None = None

    @classmethod
    async def build(
        cls,
        settings: Settings,
        bus: EventBus,
        exchanges: dict[str, ExchangeAdapter],
        feeds: dict[str, MarketDataFeed],
        order_gate: Callable[[], bool],
        http_get: HttpGet | None = None,
    ) -> TradingStack:
        cfg = settings.config
        ex_cfg = cfg.execution
        repo = await Repository.connect(settings.secrets.database_url.get_secret_value())
        kill = KillSwitch(cfg.risk, alert_sink=bus.publish)
        market_type = next(iter(exchanges.values())).market_type if exchanges else "spot"
        sizer = GrowthSizer(cfg.risk, Decimal(1))  # initial equity set in start()
        ops = OpsService(cfg, settings.mode, exchanges, bus.publish, http_get)
        risk = RiskManager(cfg, kill, sizer, order_gate, market_type, ops=ops.guard)
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
            ops=ops,
        )
        # shadow mode: changed params run virtually next to the approved (live) version
        registry = ShadowRegistry.from_dict(await repo.get_state(shadow_mod.STATE_KEY))
        plan = registry.plan(cfg, settings.mode, kill.clock.now())
        await repo.set_state(shadow_mod.STATE_KEY, registry.to_dict())
        live_cfg = plan.live_config
        strategies = [
            s
            for name in exchanges
            for sym in cfg.universe.symbols
            for s in build_enabled(live_cfg, sym, name)
        ]
        runner = StrategyRunner(bus, strategies) if strategies else None
        shadow_cfg = cfg.model_copy(update={"strategies": plan.shadow_strategies})
        shadow_strats = (
            [
                s
                for name in exchanges
                for sym in cfg.universe.symbols
                for s in build_enabled(shadow_cfg, sym, name)
            ]
            if plan.shadow_strategies
            else []
        )
        book = ShadowBook.from_dict(
            await repo.get_state(shadow_mod.BOOK_KEY), cfg.backtest.commission_pct
        )
        shadow_runner = (
            ShadowRunner(bus, shadow_strats, plan.shadow_names, book) if shadow_strats else None
        )
        arb_cfg = cfg.arbitrage
        arb: ArbitrageService | None = None
        if arb_cfg.cross_exchange.enabled or arb_cfg.triangular.enabled:
            executor = None
            if settings.mode is Mode.PAPER and arb_cfg.execute_in_paper:
                executor = ArbitrageExecutor(
                    exchanges, risk, settings.mode, lambda: PortfolioView(coord.last_equity)
                )
            arb = ArbitrageService(arb_cfg, bus, exchanges, repo, executor)
        return cls(
            repo,
            coord,
            engines,
            strategies,
            runner,
            arb,
            ops,
            registry,
            plan,
            book,
            shadow_runner,
            shadow_strats,
            ConfigChangeLog(repo, kill.clock),
        )

    async def start(
        self, settings: Settings, stop: asyncio.Event, tasks: list[asyncio.Task[None]]
    ) -> None:
        coord = self.coordinator
        await coord.restore()
        await self.start_ops(settings)
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
        if self.arbitrage is not None:
            tasks.append(asyncio.create_task(self.arbitrage.run(stop), name="arbitrage"))
        tasks.append(asyncio.create_task(self._persist_loop(stop), name="ops-persist"))
        _log.info(
            "trading_started",
            strategies=[f"{s.name}:{s.symbol}" for s in self.strategies],
            equity=str(equity),
        )

    async def start_ops(self, settings: Settings) -> None:
        if self.ops is not None:
            self.ops.capital.restore(await self.repo.get_state(capital_mod.STATE_KEY))
        if self.config_log is not None:
            n = await self.config_log.sync_file_config(settings.config)
            if n:
                _log.warning("config_changed_since_last_run", changes=n)
        for note in self.shadow_plan.notes if self.shadow_plan else []:
            _log.warning("shadow_mode", note=note)
        await self.persist_ops()

    async def persist_ops(self) -> None:
        if self.ops is not None:
            await self.repo.set_state(capital_mod.STATE_KEY, self.ops.capital.state.to_dict())
        await self.repo.set_state(shadow_mod.STATE_KEY, self.shadow_registry.to_dict())
        await self.repo.set_state(shadow_mod.BOOK_KEY, self.shadow_book.to_dict())

    async def _persist_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await wait_or_stop(stop, 300)
            try:
                await self.persist_ops()
            except Exception:
                _log.exception("ops_persist_failed")

    async def warm_up(self, settings: Settings) -> None:
        """Feed recent closed candles to strategies and the coordinator (no signals traded)."""
        bars = settings.config.marketdata.warmup_bars
        seen: set[tuple[str, str, str]] = set()
        everything = [*self.strategies, *self.shadow_strategies]
        for s in everything:
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
                for st in everything:
                    st.on_candle(c)
                await self.coordinator.on_candle(CandleEvent(candle=c))

    async def close(self) -> None:
        await self.coordinator.persist()
        await self.persist_ops()
        await self.repo.close()
