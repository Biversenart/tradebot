"""PanelData: read-only queries and live events for the web panel.

BotControl stays the single place for actions (kill switch, toggles, approvals); this class
only reads: account overview, positions with live marks, trade history and statistics, the
price chart with entry/exit markers, risk limits in use, arbitrage log and the effective
config (YAML only; secrets are never part of AppConfig).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from bot.api import insights
from bot.api.jobs import JobRunner
from bot.backtest.runner import NoDataError, load_history
from bot.control import BotControl
from bot.core.event_bus import EventBus
from bot.core.events import PositionEvent, RiskAlert
from bot.execution.coordinator import TradingCoordinator

ZERO = Decimal(0)
MAX_SUBSCRIBERS = 20


def _pct(part: Decimal, whole: Decimal) -> str | None:
    return f"{part / whole * 100:.2f}" if whole > 0 else None


class PanelData:
    def __init__(
        self,
        control: BotControl,
        coordinator: TradingCoordinator | None = None,
        jobs: JobRunner | None = None,
        bus: EventBus | None = None,
        data_root: Path = Path("data"),
        default_exchange: str | None = None,
        mark: Callable[[str, str], Decimal | None] | None = None,
    ) -> None:
        self.control = control
        self.default_exchange = default_exchange
        self._mark_fn = mark
        self.coordinator = coordinator
        self.jobs = jobs
        self.data_root = data_root
        self.cfg = control.settings.config
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        if bus is not None:
            bus.subscribe(RiskAlert, self._on_alert)
            bus.subscribe(PositionEvent, self._on_position)

    # ---------------------------------------------------------------- live events
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        if len(self._subscribers) >= MAX_SUBSCRIBERS:
            raise RuntimeError("Çok fazla canlı bağlantı.")
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def broadcast(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # slow client: drop the oldest
                q.get_nowait()
                q.put_nowait(event)

    async def _on_alert(self, a: RiskAlert) -> None:
        self.broadcast(
            {
                "type": "alert",
                "level": a.level.value,
                "code": a.code,
                "message": a.message,
                "t": a.created_at.isoformat(),
            }
        )

    async def _on_position(self, e: PositionEvent) -> None:
        self.broadcast(
            {
                "type": "position",
                "kind": e.kind,
                "symbol": e.symbol,
                "side": e.side,
                "amount": str(e.amount),
                "pnl": str(e.realized_pnl),
                "reason": e.reason,
                "t": e.created_at.isoformat(),
            }
        )

    # ---------------------------------------------------------------- helpers
    def _mark(self, exchange: str, symbol: str) -> Decimal | None:
        if self.coordinator is not None:
            return self.coordinator.mark(exchange, symbol)
        return self._mark_fn(exchange, symbol) if self._mark_fn else None

    def _equity(self) -> Decimal:
        if self.coordinator is not None and self.coordinator.last_equity > 0:
            return self.coordinator.last_equity
        return self.control._equity()

    # ---------------------------------------------------------------- queries
    async def positions(self) -> list[dict[str, Any]]:
        return [
            insights.position_view(p, self._mark(p.exchange, p.symbol))
            for p in await self.control.repo.open_positions()
        ]

    async def trades(
        self, limit: int = 200, symbol: str | None = None, strategy: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await self.control.repo.closed_positions(min(limit, 2000), symbol, strategy)
        return [insights.trade_view(p) for p in rows]

    async def overview(self) -> dict[str, Any]:
        st = (await self.control.status()).to_dict()
        positions = await self.positions()
        closed = await self.control.repo.closed_positions(5000)
        today = datetime.now(UTC).date()
        today_pnl = sum(
            (p.realized_pnl for p in closed if p.closed_at and p.closed_at.date() == today), ZERO
        )
        upnl = sum((Decimal(p["unrealized_pnl"]) for p in positions), ZERO)
        exposure = sum((Decimal(p["notional"]) for p in positions), ZERO)
        curve, max_dd = insights.drawdown(await self.control.repo.equity_curve(5000))
        equity = self._equity()
        return st | {
            "equity": str(equity),
            "today_pnl": str(today_pnl),
            "unrealized_pnl": str(upnl),
            "exposure": str(exposure),
            "exposure_pct": _pct(exposure, equity),
            "max_drawdown_pct": f"{max_dd:.2f}",
            "current_drawdown_pct": curve[-1]["dd"] if curve else "0",
            "totals": insights.total_stats(closed),
            "base_currency": self.cfg.base_currency,
            "live_confirmed": self.control.settings.sends_real_orders,
        }

    async def performance(self) -> dict[str, Any]:
        closed = await self.control.repo.closed_positions(5000)
        curve, max_dd = insights.drawdown(await self.control.repo.equity_curve(5000))
        return {
            "totals": insights.total_stats(closed),
            "by_strategy": insights.strategy_stats(closed),
            "daily_pnl": insights.daily_pnl(closed),
            "equity": curve,
            "max_drawdown_pct": f"{max_dd:.2f}",
        }

    async def strategies(self) -> list[dict[str, Any]]:
        closed = await self.control.repo.closed_positions(5000)
        stats = insights.strategy_stats(closed)
        status = (await self.control.status()).strategies
        shadow = self.control.ops_status().get("shadow", [])
        out = []
        for key, enabled in status.items():
            name, _, symbol = key.partition(":")
            sc = self.cfg.strategies.get(name)
            params = dict(sc.model_extra or {}) if sc is not None else {}
            out.append(
                {
                    "key": key,
                    "name": name,
                    "symbol": symbol,
                    "enabled": enabled,
                    "timeframe": params.get("timeframe"),
                    "params": {k: str(v) for k, v in params.items()},
                    "stats": stats.get(name),
                    "shadow": next((s for s in shadow if s.startswith(f"{name}:")), None),
                }
            )
        return out

    def symbols(self) -> list[str]:
        syms = list(self.cfg.universe.symbols)
        for p in self.cfg.arbitrage.cross_exchange.pairs:
            if p not in syms:
                syms.append(p)
        return syms

    async def candles(
        self, symbol: str, timeframe: str = "1h", exchange: str | None = None, limit: int = 500
    ) -> dict[str, Any]:
        limit = max(10, min(limit, 3000))
        ex = exchange or self.default_exchange or next(iter(self.cfg.exchanges), "binance")
        df: pd.DataFrame | None = None
        source = "canlı"
        if self.coordinator is not None:
            for name in [ex, *self.coordinator.engines]:
                df = self.coordinator.frame(name, symbol, timeframe)
                if df is not None and len(df):
                    break
        if df is None or not len(df):
            source = "geçmiş veri (Parquet)"
            try:
                df = load_history(self.data_root, ex, symbol, timeframe)
            except NoDataError:
                df = None
        bars: list[dict[str, Any]] = []
        if df is not None:
            tail = df.iloc[-limit:]
            idx = pd.DatetimeIndex(tail.index)
            for i, ts in enumerate(idx):
                bars.append(
                    {
                        "t": ts.isoformat(),
                        "o": float(tail["open"].iloc[i]),
                        "h": float(tail["high"].iloc[i]),
                        "l": float(tail["low"].iloc[i]),
                        "c": float(tail["close"].iloc[i]),
                        "v": float(tail["volume"].iloc[i]),
                    }
                )
        positions = await self.control.repo.positions_for_symbol(symbol)
        orders = await self.control.repo.filled_orders([p.position_id for p in positions])
        open_lines = [
            {
                "side": p.side,
                "entry": str(p.entry_price),
                "stop": str(p.stop_loss),
                "targets": p.take_profits,
            }
            for p in positions
            if p.status == "open"
        ]
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": source if bars else "veri yok",
            "bars": bars,
            "markers": insights.markers(positions, orders),
            "open": open_lines,
        }

    async def risk(self) -> dict[str, Any]:
        ks = self.control.kill_switch
        st, r = ks.state, self.cfg.risk
        equity = self._equity()
        daily_loss = max(ZERO, st.day_start_equity - equity)
        dd = max(ZERO, st.peak_equity - equity)
        positions = await self.positions()
        by_symbol: dict[str, Decimal] = {}
        for p in positions:
            by_symbol[p["symbol"]] = by_symbol.get(p["symbol"], ZERO) + Decimal(p["notional"])
        out: dict[str, Any] = {
            "kill_switch": {
                "active": ks.is_active,
                "reasons": dict(st.reasons),
                "close_requested": st.close_requested,
                "triggered_at": insights.iso(st.triggered_at),
                "on_kill_switch": r.on_kill_switch,
            },
            "limits": [
                {
                    "name": "Günlük zarar",
                    "value_pct": _pct(daily_loss, st.day_start_equity) or "0",
                    "limit_pct": str(r.daily_loss_limit_pct),
                },
                {
                    "name": "Drawdown",
                    "value_pct": _pct(dd, st.peak_equity) or "0",
                    "limit_pct": str(r.max_drawdown_pct),
                },
                {
                    "name": "Toplam maruziyet",
                    "value_pct": _pct(sum(by_symbol.values(), ZERO), equity) or "0",
                    "limit_pct": str(r.max_total_exposure_pct),
                },
            ],
            "errors": {"consecutive": st.consecutive_errors, "max": r.max_consecutive_errors},
            "open_positions": {"value": len(positions), "max": r.max_open_positions},
            "exposure": [
                {
                    "symbol": s,
                    "notional": str(v),
                    "pct": _pct(v, equity),
                    "limit_pct": str(r.max_exposure_per_symbol_pct),
                }
                for s, v in sorted(by_symbol.items())
            ],
            "settings": {
                "risk_per_trade_pct": str(r.risk_per_trade_pct),
                "max_leverage": r.max_leverage,
                "max_spread_pct": str(r.max_spread_pct),
                "max_correlation": str(r.max_correlation),
            },
            "ops": self.control.ops_status(),
        }
        if self.coordinator is not None:
            sz = self.coordinator.sizer
            b = sz.last
            out["sizing"] = {
                "initial_equity": str(sz.initial_equity),
                "peak": str(sz.peak),
                "reserve": str(sz.reserve),
                "loss_streak": sz.tracker.loss_streak,
                "last": None
                if b is None
                else {
                    "base": str(b.base),
                    "quality": str(b.quality),
                    "drawdown": str(b.drawdown),
                    "loss_streak": str(b.loss_streak),
                    "event": str(b.event),
                    "allocation": str(b.allocation),
                    "final_risk_pct": f"{b.final_risk_pct:.3f}",
                    "paused_reason": b.paused_reason,
                    "notes": b.notes,
                },
            }
        return out

    async def arbitrage(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self.control.repo.recent_arbitrage(min(limit, 1000))
        return [
            {
                "t": a.timestamp.isoformat(),
                "kind": a.kind,
                "route": a.route,
                "net_pct": f"{a.net_pct:.3f}",
                "size_quote": str(a.size_quote),
                "executed": a.executed,
            }
            for a in rows
        ]

    def config(self) -> dict[str, Any]:
        s = self.control.settings
        return {
            "mode": str(s.mode),
            "sends_real_orders": s.sends_real_orders,
            "config": s.config.model_dump(mode="json"),
        }
