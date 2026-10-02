"""Shadow mode (spec §5.13): changed strategy parameters run as paper next to the live version.

On testnet/live with `operations.shadow_mode`, every strategy's parameters are compared with
the last *approved* parameters (persisted). Unchanged -> runs live. Changed -> the live slot
keeps running the approved parameters and the new parameters run in shadow (virtual trades,
never orders). New strategies run shadow-only. Disabling a strategy takes effect immediately
(risk-reducing). Promotion is manual: `bot shadow approve <strategy>` after the comparison
report; the next restart runs the new parameters live.

Both versions are scored by the same virtual-trade rule (entry = plan mid, exit at stop or the
final target, stop checked first on the bar), so the comparison is apples to apples even
though real fills differ. The very first testnet/live start records the current parameters as
the approved baseline (they have just been validated in paper / backtest).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from bot.config.schema import AppConfig, Mode, StrategyConfig
from bot.core.event_bus import EventBus
from bot.core.events import CandleEvent, SignalEvent
from bot.core.models import Candle, PositionSide, Signal
from bot.log import get_logger
from bot.strategies.base import BaseStrategy

ZERO = Decimal(0)
STATE_KEY = "shadow_registry"
BOOK_KEY = "shadow_book"
LIVE, SHADOW = "live", "shadow"
_log = get_logger(__name__)


def params_of(sc: StrategyConfig) -> dict[str, Any]:
    return dict(json.loads(sc.model_dump_json()))


def fingerprint(params: dict[str, Any]) -> str:
    raw = json.dumps(params, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:12]


@dataclass
class ShadowPlan:
    live_config: AppConfig  # config whose `strategies` are the approved (live) params
    shadow_strategies: dict[str, StrategyConfig] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def shadow_names(self) -> set[str]:
        return set(self.shadow_strategies)


@dataclass
class ShadowRegistry:
    approved: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"approved": self.approved, "history": self.history}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ShadowRegistry:
        if not d:
            return cls()
        return cls(dict(d.get("approved", {})), list(d.get("history", [])))

    def plan(self, cfg: AppConfig, mode: Mode, now: datetime) -> ShadowPlan:
        if not cfg.operations.shadow_mode or mode not in (Mode.TESTNET, Mode.LIVE):
            return ShadowPlan(cfg)
        if not self.approved:
            for name, sc in cfg.strategies.items():
                self.approved[name] = params_of(sc)
            self.history.append({"at": now.isoformat(), "event": "baseline", "by": "system"})
            return ShadowPlan(
                cfg, notes=["gölge mod: mevcut parametreler onaylı temel kabul edildi"]
            )
        live: dict[str, StrategyConfig] = {}
        shadow: dict[str, StrategyConfig] = {}
        notes: list[str] = []
        for name, sc in cfg.strategies.items():
            cur = params_of(sc)
            appr = self.approved.get(name)
            if not sc.enabled:
                live[name] = sc  # disabling is immediate
                continue
            if appr is None:
                shadow[name] = sc
                notes.append(f"{name}: yeni strateji, yalnızca gölgede")
                continue
            if fingerprint(appr) == fingerprint(cur):
                live[name] = sc
                continue
            shadow[name] = sc
            live[name] = StrategyConfig.model_validate(appr)
            notes.append(
                f"{name}: parametre değişti ({fingerprint(appr)} -> {fingerprint(cur)}); "
                "canlıda onaylı sürüm, yeni sürüm gölgede"
            )
        return ShadowPlan(cfg.model_copy(update={"strategies": live}), shadow, notes)

    def approve(self, name: str, cfg: AppConfig, operator: str, now: datetime) -> str:
        sc = cfg.strategies.get(name)
        if sc is None:
            raise KeyError(name)
        old = self.approved.get(name)
        self.approved[name] = params_of(sc)
        self.history.append(
            {
                "at": now.isoformat(),
                "event": "approve",
                "strategy": name,
                "old": fingerprint(old) if old else "-",
                "new": fingerprint(self.approved[name]),
                "by": operator,
            }
        )
        return f"{name} onaylandı ({fingerprint(self.approved[name])}); yeniden başlatınca canlıda."


# ------------------------------------------------------------------- virtual book
@dataclass
class VirtualTrade:
    version: str
    strategy: str
    symbol: str
    timeframe: str
    long: bool
    entry: Decimal
    stop: Decimal
    target: Decimal
    opened_at: datetime
    exit: Decimal | None = None
    r: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if v is not None else None) for k, v in self.__dict__.items()} | {
            "long": self.long,
            "opened_at": self.opened_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VirtualTrade:
        def dec(v: Any) -> Decimal | None:
            return Decimal(v) if v is not None else None

        return cls(
            d["version"],
            d["strategy"],
            d["symbol"],
            d["timeframe"],
            bool(d["long"]),
            Decimal(d["entry"]),
            Decimal(d["stop"]),
            Decimal(d["target"]),
            datetime.fromisoformat(d["opened_at"]),
            dec(d.get("exit")),
            dec(d.get("r")),
        )


@dataclass(frozen=True)
class ShadowStats:
    trades: int
    win_rate: float
    expectancy_r: float
    total_r: float


@dataclass
class ShadowBook:
    fee_pct: Decimal = Decimal("0.1")  # per side
    open: dict[tuple[str, str, str], VirtualTrade] = field(default_factory=dict)
    closed: list[VirtualTrade] = field(default_factory=list)

    def on_signal(self, version: str, sig: Signal) -> None:
        plan = sig.plan
        key = (version, sig.strategy, sig.symbol)
        if plan is None or key in self.open:
            return
        long = sig.side is PositionSide.LONG
        entry = (plan.entry_low + plan.entry_high) / 2
        if (long and plan.stop_loss >= entry) or (not long and plan.stop_loss <= entry):
            return
        self.open[key] = VirtualTrade(
            version,
            sig.strategy,
            sig.symbol,
            sig.timeframe,
            long,
            entry,
            plan.stop_loss,
            plan.take_profits[-1],
            sig.timestamp,
        )

    def on_candle(self, c: Candle) -> None:
        if not c.closed:
            return
        for key, t in list(self.open.items()):
            if t.symbol != c.symbol or t.timeframe != c.timeframe or c.open_time < t.opened_at:
                continue
            hit_stop = c.low <= t.stop if t.long else c.high >= t.stop
            hit_tp = c.high >= t.target if t.long else c.low <= t.target
            if not (hit_stop or hit_tp):
                continue
            t.exit = t.stop if hit_stop else t.target  # stop first: conservative
            risk = abs(t.entry - t.stop)
            gross = (t.exit - t.entry) if t.long else (t.entry - t.exit)
            fees = (t.entry + t.exit) * self.fee_pct / 100
            t.r = (gross - fees) / risk if risk > 0 else ZERO
            self.closed.append(t)
            del self.open[key]
        self.closed = self.closed[-2000:]

    def stats(self, version: str, strategy: str) -> ShadowStats:
        rs = [
            float(t.r)
            for t in self.closed
            if t.version == version and t.strategy == strategy and t.r is not None
        ]
        if not rs:
            return ShadowStats(0, 0.0, 0.0, 0.0)
        wins = sum(1 for r in rs if r > 0)
        return ShadowStats(len(rs), wins / len(rs), sum(rs) / len(rs), sum(rs))

    def compare_tr(self, strategy: str, min_trades: int) -> str:
        live, sh = self.stats(LIVE, strategy), self.stats(SHADOW, strategy)
        verdict = (
            f"yetersiz veri (gölge {sh.trades}/{min_trades} işlem)"
            if sh.trades < min_trades
            else (
                "gölge sürüm daha iyi/eşit — onay değerlendirilebilir"
                if sh.expectancy_r >= live.expectancy_r
                else "gölge sürüm daha kötü — onaylamayın"
            )
        )
        return (
            f"{strategy}: canlı sürüm {live.trades} işlem, beklenti {live.expectancy_r:+.2f}R, "
            f"isabet %{live.win_rate * 100:.0f} | gölge {sh.trades} işlem, beklenti "
            f"{sh.expectancy_r:+.2f}R, isabet %{sh.win_rate * 100:.0f} -> {verdict}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "open": [t.to_dict() for t in self.open.values()],
            "closed": [t.to_dict() for t in self.closed],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, fee_pct: Decimal = Decimal("0.1")) -> ShadowBook:
        book = cls(fee_pct)
        if d:
            for raw in d.get("open", []):
                t = VirtualTrade.from_dict(raw)
                book.open[(t.version, t.strategy, t.symbol)] = t
            book.closed = [VirtualTrade.from_dict(x) for x in d.get("closed", [])]
        return book


class ShadowRunner:
    """Feeds shadow strategies and the virtual book; never publishes signals or orders."""

    def __init__(
        self,
        bus: EventBus,
        shadow_strategies: list[BaseStrategy],
        tracked: set[str],
        book: ShadowBook,
    ) -> None:
        self.strategies = shadow_strategies
        self.tracked = tracked  # strategy names whose live version is also scored
        self.book = book
        bus.subscribe(CandleEvent, self.on_candle)
        bus.subscribe(SignalEvent, self.on_live_signal)

    async def on_live_signal(self, event: SignalEvent) -> None:
        if event.signal.strategy in self.tracked:
            self.book.on_signal(LIVE, event.signal)

    async def on_candle(self, event: CandleEvent) -> None:
        c = event.candle
        if not c.closed:
            return
        self.book.on_candle(c)
        for s in self.strategies:
            if s.symbol != c.symbol or s.timeframe != c.timeframe:
                continue
            s.on_candle(c)
            try:
                signals = s.generate_signals()
            except Exception:
                _log.exception("shadow_strategy_failed", strategy=s.name)
                continue
            for sig in signals:
                self.book.on_signal(SHADOW, sig)
