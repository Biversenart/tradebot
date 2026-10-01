"""Live trading loop: Signal -> RiskManager -> ExecutionEngine, plus position management.

- SignalEvent: build PortfolioView + MarketContext, ask RiskManager, open the position.
- CandleEvent (closed, position timeframe): TP partials, break-even, trailing, time exit
  (same rules as `bot.portfolio.exits` / the backtest). Stop-loss itself lives ON the exchange.
- Maintenance loop: sync exchange stops (detect stop fills), mark equity -> kill switch,
  sizer and equity curve; persist kill-switch state; close everything if the kill switch
  requests it.
- RiskAlert: persisted to `risk_events`.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd

from bot.config.schema import AppConfig
from bot.core.aio import wait_or_stop
from bot.core.clock import Clock, SystemClock
from bot.core.event_bus import EventBus
from bot.core.events import CandleEvent, PositionEvent, RiskAlert, SignalEvent
from bot.core.models import Candle, OrderIntent, OrderType, PositionSide, Side
from bot.exchanges.errors import ExchangeAdapterError
from bot.execution.engine import ExecutionEngine
from bot.indicators import atr as atr_ind
from bot.log import get_logger
from bot.marketdata.feed import MarketDataFeed
from bot.risk.kill_switch import KillSwitch, KillSwitchState
from bot.risk.manager import MarketContext, RiskManager
from bot.risk.portfolio import PortfolioView, PositionView
from bot.risk.sizing import GrowthSizer
from bot.storage.models import PositionRow
from bot.storage.repository import Repository

ZERO = Decimal(0)
KILL_STATE_KEY = "kill_switch"
_log = get_logger(__name__)


@dataclass(frozen=True)
class CoordinatorConfig:
    maintenance_interval_seconds: float = 30.0
    candle_buffer: int = 800


class TradingCoordinator:
    def __init__(
        self,
        config: AppConfig,
        bus: EventBus,
        repo: Repository,
        risk: RiskManager,
        engines: dict[str, ExecutionEngine],
        feeds: dict[str, MarketDataFeed] | None = None,
        clock: Clock | None = None,
        coord: CoordinatorConfig | None = None,
    ) -> None:
        self.cfg = config
        self.bus = bus
        self.repo = repo
        self.risk = risk
        self.kill_switch: KillSwitch = risk.kill_switch
        self.sizer: GrowthSizer = risk.sizer
        self.engines = engines
        self.feeds = feeds or {}
        self.clock = clock or SystemClock()
        self.coord = coord or CoordinatorConfig()
        self._candles: dict[tuple[str, str, str], deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self.coord.candle_buffer)
        )
        self._last_close: dict[tuple[str, str], Decimal] = {}
        self._closing_all = False
        self.last_equity = ZERO
        self._lock = asyncio.Lock()
        bus.subscribe(SignalEvent, self.on_signal)
        bus.subscribe(CandleEvent, self.on_candle)
        bus.subscribe(RiskAlert, self.on_alert)

    # ---------------------------------------------------------------- state
    async def restore(self) -> None:
        data = await self.repo.get_state(KILL_STATE_KEY)
        if data:
            self.kill_switch.state = KillSwitchState.from_dict(data)
            _log.info("kill_switch_restored", reasons=list(self.kill_switch.state.reasons))

    async def persist(self) -> None:
        await self.repo.set_state(KILL_STATE_KEY, self.kill_switch.state.to_dict())

    # ---------------------------------------------------------------- market data helpers
    def frame(self, exchange: str, symbol: str, timeframe: str) -> pd.DataFrame | None:
        rows = self._candles.get((exchange, symbol, timeframe))
        if not rows:
            return None
        df = pd.DataFrame(list(rows))
        return df.set_index("open_time")

    def mark(self, exchange: str, symbol: str) -> Decimal | None:
        feed = self.feeds.get(exchange)
        if feed is not None and symbol in feed.tickers:
            return feed.tickers[symbol].last
        return self._last_close.get((exchange, symbol))

    async def portfolio(self, exchange: str | None = None) -> PortfolioView:
        positions: list[PositionView] = []
        for p in await self.repo.open_positions(exchange):
            m = self.mark(p.exchange, p.symbol) or p.entry_price
            positions.append(
                PositionView(
                    p.symbol,
                    PositionSide(p.side),
                    p.amount,
                    p.entry_price,
                    p.stop_loss,
                    m,
                    p.strategy,
                )
            )
        equity, balances = await self.equity()
        return PortfolioView(equity=equity, positions=positions, balances=balances)

    async def equity(self) -> tuple[Decimal, dict[str, Decimal]]:
        """Account value in quote currency across exchanges (spot balances marked to market)."""
        total = ZERO
        values: dict[str, Decimal] = {}
        base_ccy = self.cfg.base_currency
        for name, eng in self.engines.items():
            try:
                bal = await eng.adapter.fetch_balance()
            except ExchangeAdapterError as exc:
                _log.warning("balance_failed", exchange=name, error=str(exc))
                continue
            for asset, b in bal.items():
                if asset == base_ccy or asset in self.cfg.risk.stablecoins:
                    v = b.total
                else:
                    px = self.mark(name, f"{asset}/{base_ccy}")
                    v = b.total * px if px is not None else ZERO
                values[asset] = values.get(asset, ZERO) + v
                total += v
        if total > 0:
            self.last_equity = total
        return total, values

    # ---------------------------------------------------------------- events
    async def on_alert(self, alert: RiskAlert) -> None:
        try:
            await self.repo.save_risk_event(alert)
        except Exception:
            _log.exception("risk_event_persist_failed")
        if alert.code.startswith("egress_ip_"):
            await self.kill_switch.set_egress_ok(alert.code == "egress_ip_recovered", alert.code)
        await self.persist()

    async def on_candle(self, event: CandleEvent) -> None:
        c = event.candle
        if not c.closed:
            return
        key = (c.exchange, c.symbol, c.timeframe)
        self._candles[key].append(
            {
                "open_time": pd.Timestamp(c.open_time),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": float(c.volume),
            }
        )
        self._last_close[(c.exchange, c.symbol)] = c.close
        for pos in await self.repo.open_positions(c.exchange):
            if pos.symbol == c.symbol and pos.timeframe == c.timeframe:
                try:
                    await self.manage_position(pos, c)
                except ExchangeAdapterError as exc:
                    _log.warning("manage_position_failed", position=pos.position_id, error=str(exc))

    async def on_signal(self, event: SignalEvent) -> None:
        sig = event.signal
        eng = self.engines.get(sig.exchange)
        if eng is None or sig.plan is None:
            return
        async with self._lock:
            await self.repo.save_signal(sig)
            feed = self.feeds.get(sig.exchange)
            closes = {}
            for (ex, sym, tf), rows in self._candles.items():
                if ex == sig.exchange and tf == sig.timeframe and rows:
                    closes[sym] = pd.Series(
                        [r["close"] for r in rows], index=[r["open_time"] for r in rows]
                    )
            ctx = MarketContext(
                market=eng.market(sig.symbol),
                ticker=feed.tickers.get(sig.symbol) if feed else None,
                book=feed.books.get(sig.symbol) if feed else None,
                candles=self.frame(sig.exchange, sig.symbol, sig.timeframe),
                closes=closes,
            )
            pv = await self.portfolio()
            decision = self.risk.assess_signal(sig, pv, ctx)
            await self.repo.save_risk_report(decision.report.to_dict(), sig.signal_id)
            if decision.approved is None:
                _log.info("signal_rejected", symbol=sig.symbol, reasons=decision.report.reasons)
                return
            try:
                await eng.open_position(
                    decision.approved, list(sig.plan.take_profits), sig.timeframe
                )
            except ExchangeAdapterError as exc:
                _log.warning("open_position_failed", symbol=sig.symbol, error=str(exc))

    # ---------------------------------------------------------------- position management
    def _exit_intent(self, pos: PositionRow, qty: Decimal, reason: str) -> OrderIntent:
        long = pos.side == PositionSide.LONG.value
        return OrderIntent(
            intent_id=f"{pos.position_id}-{reason}-{pos.targets_hit}",
            exchange=pos.exchange,
            symbol=pos.symbol,
            side=Side.SELL if long else Side.BUY,
            order_type=OrderType.MARKET,
            amount=qty,
            reduce_only=True,
            strategy=pos.strategy,
            reason=reason,
        )

    async def _exit(
        self, pos: PositionRow, qty: Decimal, reason: str, new_stop: Decimal | None = None
    ) -> None:
        decision = self.risk.assess_exit(self._exit_intent(pos, qty, reason), pos.amount)
        if decision.approved is None:
            _log.warning("exit_rejected", reasons=decision.report.reasons)
            return
        before = pos.realized_pnl
        await self.engines[pos.exchange].reduce(pos, decision.approved, reason, new_stop)
        await self._publish(pos, "closed" if pos.status == "closed" else "reduced", None, reason)
        if pos.status == "closed":
            await self._on_closed(pos, pos.realized_pnl - before)

    async def _publish(
        self, pos: PositionRow, kind: str, price: Decimal | None, reason: str
    ) -> None:
        await self.bus.publish(
            PositionEvent(
                kind=kind,
                position_id=pos.position_id,
                exchange=pos.exchange,
                symbol=pos.symbol,
                side=pos.side,
                amount=pos.amount if kind != "closed" else pos.initial_amount,
                price=price,
                realized_pnl=pos.realized_pnl,
                reason=reason,
            )
        )

    async def _on_closed(self, pos: PositionRow, _last_leg: Decimal) -> None:
        risk_amt = abs(pos.entry_price - pos.initial_stop) * pos.initial_amount
        r = pos.realized_pnl / risk_amt if risk_amt > 0 else ZERO
        self.sizer.on_trade_closed(pos.strategy, pos.symbol, pos.realized_pnl, r)

    async def manage_position(self, pos: PositionRow, c: Candle) -> None:
        tp = self.cfg.analysis.trade_plan
        long = pos.side == PositionSide.LONG.value
        pos.bars_open += 1
        targets = [Decimal(t) for t in pos.take_profits]
        hit = pos.targets_hit < len(targets) and (
            (long and c.high >= targets[pos.targets_hit])
            or (not long and c.low <= targets[pos.targets_hit])
        )
        if hit:
            n = pos.targets_hit + 1
            if n == len(targets):
                qty = pos.amount
            elif n == 1:
                qty = min(pos.amount, pos.initial_amount * tp.tp1_close_pct / 100)
            else:
                qty = pos.amount / 2
            be = pos.entry_price if n == 1 and tp.move_sl_to_breakeven_after_tp1 else None
            new_stop = (
                be
                if be is not None
                and ((long and be > pos.stop_loss) or (not long and be < pos.stop_loss))
                else None
            )
            pos.targets_hit = n
            await self.repo.save_position(pos)
            await self._exit(pos, qty, f"tp{n}", new_stop)
            return
        best = pos.best_price or pos.entry_price
        pos.best_price = max(best, c.close) if long else min(best, c.close)
        if pos.targets_hit >= 1 and tp.trailing != "none":
            df = self.frame(pos.exchange, pos.symbol, pos.timeframe)
            cand: Decimal | None = None
            if tp.trailing == "atr" and df is not None and len(df) > 20:
                a = atr_ind(df, self.cfg.analysis.indicators.atr_period).dropna()
                if not a.empty:
                    m = self.cfg.backtest.trailing_atr_mult * Decimal(str(float(a.iloc[-1])))
                    cand = pos.best_price - m if long else pos.best_price + m
            elif tp.trailing == "structure" and df is not None:
                n_bars = self.cfg.backtest.structure_trail_bars
                cand = (
                    Decimal(str(float(df["low"].iloc[-n_bars:].min())))
                    if long
                    else Decimal(str(float(df["high"].iloc[-n_bars:].max())))
                )
            if cand is not None and (
                (long and cand > pos.stop_loss) or (not long and cand < pos.stop_loss)
            ):
                await self.engines[pos.exchange].move_stop(pos, cand)
        if pos.targets_hit == 0 and pos.bars_open >= tp.time_exit_bars and pos.status == "open":
            await self._exit(pos, pos.amount, "time")
            return
        await self.repo.save_position(pos)

    async def close_all(self, reason: str) -> None:
        if self._closing_all:
            return
        self._closing_all = True
        try:
            for pos in await self.repo.open_positions():
                try:
                    await self._exit(pos, pos.amount, reason)
                except ExchangeAdapterError as exc:
                    _log.error("close_all_failed", position=pos.position_id, error=str(exc))
        finally:
            self._closing_all = False

    # ---------------------------------------------------------------- maintenance
    async def tick(self) -> None:
        for eng in self.engines.values():
            for pos in await self.repo.open_positions(eng.adapter.name):
                try:
                    if await eng.sync_stop(pos):
                        await self._publish(pos, "closed", None, pos.close_reason or "stop")
                        await self._on_closed(pos, ZERO)
                except ExchangeAdapterError as exc:
                    _log.warning("sync_stop_failed", position=pos.position_id, error=str(exc))
        equity, _ = await self.equity()
        if equity > 0:
            await self.kill_switch.record_equity(equity)
            self.sizer.on_equity(equity)
            await self.repo.save_equity(equity, self.sizer.reserve)
        await self.persist()
        if self.kill_switch.close_positions_requested:
            await self.close_all("kill_switch")

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                _log.exception("maintenance_failed")
            await wait_or_stop(stop, self.coord.maintenance_interval_seconds)
