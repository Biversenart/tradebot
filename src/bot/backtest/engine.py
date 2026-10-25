"""Event-driven backtest engine (spec §5.9).

- Uses the SAME strategy classes as live trading (`BaseStrategy.compute`, causal).
- A signal on the close of bar t is filled at the OPEN of bar t + latency (>= 1): no look-ahead.
- Fills pay slippage (bps, against us) and commission (% of notional, per side).
- Exits via `bot.portfolio.exits` (identical rules to live): stop first, partial TPs,
  break-even, trailing, time exit.
- Money is Decimal; indicator columns are floats.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

import numpy as np
import pandas as pd

from bot.config.schema import AppConfig, BacktestConfig
from bot.core.models import PositionSide, Signal
from bot.portfolio.exits import ExitFill, ExitRules, ManagedPosition, on_bar
from bot.strategies.base import BaseStrategy

ZERO = Decimal(0)
D = Decimal


def dec(x: float) -> Decimal:
    return Decimal(repr(float(x)))


class Sizer(Protocol):
    def size(self, equity: Decimal, entry: Decimal, stop: Decimal, signal: Signal) -> Decimal: ...


@dataclass
class FixedFractionalSizer:
    """Risk `risk_pct` of current equity per trade; notional capped at `max_notional_pct`."""

    risk_pct: Decimal = field(default_factory=lambda: Decimal(1))
    max_notional_pct: Decimal = field(default_factory=lambda: Decimal(100))

    def size(self, equity: Decimal, entry: Decimal, stop: Decimal, signal: Signal) -> Decimal:
        risk_unit = abs(entry - stop)
        if risk_unit <= 0 or equity <= 0:
            return ZERO
        qty = equity * self.risk_pct / 100 / risk_unit
        cap = equity * self.max_notional_pct / 100 / entry
        return min(qty, cap)


@dataclass
class TradeRecord:
    symbol: str
    strategy: str
    side: PositionSide
    entry_time: datetime
    entry_price: Decimal
    qty: Decimal
    initial_stop: Decimal
    exit_time: datetime | None = None
    exit_price: Decimal | None = None  # quantity-weighted average
    pnl: Decimal = ZERO  # net of fees
    fees: Decimal = ZERO
    exit_reason: str = ""
    bars_held: int = 0
    exits: list[ExitFill] = field(default_factory=list)
    score: Decimal = ZERO

    @property
    def risk_amount(self) -> Decimal:
        return abs(self.entry_price - self.initial_stop) * self.qty

    @property
    def r_multiple(self) -> Decimal:
        r = self.risk_amount
        return self.pnl / r if r > 0 else ZERO


@dataclass
class BacktestResult:
    trades: list[TradeRecord]
    equity: pd.Series  # mark-to-market equity (float) indexed by bar time
    initial_equity: Decimal
    config: BacktestConfig
    strategy: str
    symbols: list[str]
    bars_per_year: float

    @property
    def final_equity(self) -> Decimal:
        return dec(float(self.equity.iloc[-1])) if not self.equity.empty else self.initial_equity


@dataclass
class _Pending:
    signal: Signal
    due_bar: int
    signal_bar: int


@dataclass
class _Open:
    pos: ManagedPosition
    record: TradeRecord


class Backtester:
    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        strategy_factory: Callable[[str], BaseStrategy],
        config: AppConfig | None = None,
        sizer: Sizer | None = None,
        trade_start: pd.Timestamp | None = None,
        trade_end: pd.Timestamp | None = None,
        max_open_positions: int | None = None,
    ) -> None:
        self.app = config or AppConfig()
        self.cfg = self.app.backtest
        self.data = {s: df.sort_index() for s, df in data.items()}
        self.factory = strategy_factory
        risk = self.app.risk
        self.sizer: Sizer = sizer or FixedFractionalSizer(
            risk.risk_per_trade_pct, risk.max_exposure_per_symbol_pct * 5
        )
        self.trade_start = trade_start
        self.trade_end = trade_end
        self.max_open = max_open_positions or risk.max_open_positions
        tp = self.app.analysis.trade_plan
        self.rules = ExitRules(
            tp1_close_pct=tp.tp1_close_pct,
            move_sl_to_breakeven_after_tp1=tp.move_sl_to_breakeven_after_tp1,
            trailing=tp.trailing,
            trailing_atr_mult=self.cfg.trailing_atr_mult,
            structure_trail_bars=self.cfg.structure_trail_bars,
            time_exit_bars=tp.time_exit_bars,
        )
        self.fee_rate = self.cfg.commission_pct / 100
        self.slip = self.cfg.slippage_bps / 10_000

    # ---------------------------------------------------------------- fills
    def _slipped(self, price: Decimal, buy: bool) -> Decimal:
        return price * (1 + self.slip) if buy else price * (1 - self.slip)

    def run(self) -> BacktestResult:
        symbols = list(self.data)
        strategies = {s: self.factory(s) for s in symbols}
        name = next(iter(strategies.values())).name if strategies else "-"
        frames = {s: strategies[s].compute(df) for s, df in self.data.items()}
        times = sorted(set().union(*(df.index for df in self.data.values())))
        pos_in = {s: {t: i for i, t in enumerate(df.index)} for s, df in self.data.items()}
        arrays = {
            s: {c: df[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close")}
            for s, df in self.data.items()
        }
        atr = {s: frames[s]["atr"].to_numpy(dtype=float) for s in symbols}
        sigs = {s: frames[s]["signal"].to_numpy(dtype=int) for s in symbols}
        trail_n = self.rules.structure_trail_bars
        trail_lo = {
            s: df["low"].rolling(trail_n, min_periods=1).min().to_numpy()
            for s, df in self.data.items()
        }
        trail_hi = {
            s: df["high"].rolling(trail_n, min_periods=1).max().to_numpy()
            for s, df in self.data.items()
        }

        realized = self.cfg.initial_equity
        open_: list[_Open] = []
        pending: list[tuple[str, _Pending]] = []
        trades: list[TradeRecord] = []
        last_close: dict[str, Decimal] = {}
        eq_index: list[pd.Timestamp] = []
        eq_values: list[float] = []

        def equity_now() -> Decimal:
            return realized + sum(
                (o.pos.unrealized(last_close.get(o.pos.symbol, o.pos.entry_price)) for o in open_),
                ZERO,
            )

        for t in times:
            in_window = (self.trade_start is None or t >= self.trade_start) and (
                self.trade_end is None or t < self.trade_end
            )
            for s in symbols:
                i = pos_in[s].get(t)
                if i is None:
                    continue
                a = arrays[s]
                o, h, lo, c = (
                    dec(a["open"][i]),
                    dec(a["high"][i]),
                    dec(a["low"][i]),
                    dec(a["close"][i]),
                )
                # 1) pending entries due on this bar
                for sym, pend in [p for p in pending if p[0] == s and p[1].due_bar == i]:
                    pending.remove((sym, pend))
                    rec = self._enter(pend.signal, o, t, i, realized_equity=equity_now())
                    if rec is not None:
                        open_.append(rec)
                        realized -= rec.record.fees
                # 2) manage open positions
                atr_i = dec(atr[s][i]) if atr[s][i] == atr[s][i] else None
                for op in [x for x in open_ if x.pos.symbol == s]:
                    trail = dec(trail_lo[s][i]) if op.pos.is_long else dec(trail_hi[s][i])
                    fills = on_bar(op.pos, self.rules, i, o, h, lo, c, atr_i, trail)
                    for f in fills:
                        realized += self._book_exit(op, f, t, i)
                    if not op.pos.is_open:
                        open_.remove(op)
                        trades.append(op.record)
                last_close[s] = c
                # 3) new signal at the close of this bar
                if not in_window:
                    continue
                direction = int(sigs[s][i])
                if direction == 0:
                    continue
                if direction < 0 and not (self.cfg.allow_short and strategies[s].allow_short):
                    continue
                row = frames[s].iloc[i]
                same = sum(1 for x in open_ if x.pos.symbol == s) + sum(
                    1 for p in pending if p[0] == s
                )
                if (
                    same >= strategies[s].max_positions
                    or len(open_) + len(pending) >= self.max_open
                ):
                    continue
                sig = strategies[s].signal_from_row(pd.Timestamp(t), float(a["close"][i]), row)
                if sig is not None:
                    pending.append((s, _Pending(sig, i + self.cfg.latency_bars, i)))
            if in_window or open_:
                eq_index.append(pd.Timestamp(t))
                eq_values.append(float(equity_now()))
            if self.trade_end is not None and t >= self.trade_end and not open_:
                break
        # close anything still open at the last available close
        for op in list(open_):
            px = last_close.get(op.pos.symbol, op.pos.entry_price)
            f = ExitFill(px, op.pos.qty, "end_of_data")
            op.pos.qty = ZERO
            op.pos.fills.append(f)
            realized += self._book_exit(op, f, times[-1], len(self.data[op.pos.symbol]) - 1)
            trades.append(op.record)
        if eq_values:
            eq_values[-1] = float(realized)
        equity = pd.Series(eq_values, index=pd.DatetimeIndex(eq_index), dtype=float)
        return BacktestResult(
            trades=sorted(trades, key=lambda r: r.entry_time),
            equity=equity,
            initial_equity=self.cfg.initial_equity,
            config=self.cfg,
            strategy=name,
            symbols=symbols,
            bars_per_year=_bars_per_year(self.data),
        )

    def _enter(
        self, signal: Signal, open_px: Decimal, t: pd.Timestamp, i: int, realized_equity: Decimal
    ) -> _Open | None:
        plan = signal.plan
        if plan is None:
            return None
        long = plan.side is PositionSide.LONG
        entry = self._slipped(open_px, buy=long)
        stop = plan.stop_loss
        if (long and entry <= stop) or (not long and entry >= stop):
            return None  # gapped through the stop before entry
        # targets keep their R distances relative to the actual entry
        risk = abs(entry - stop)
        sgn = 1 if long else -1
        targets = [entry + sgn * r * risk for r in plan.reward_risk_ratios]
        qty = self.sizer.size(realized_equity, entry, stop, signal)
        if qty <= 0:
            return None
        fee = entry * qty * self.fee_rate
        pos = ManagedPosition(
            symbol=plan.symbol,
            strategy=signal.strategy,
            side=plan.side,
            entry_price=entry,
            initial_qty=qty,
            initial_stop=stop,
            stop=stop,
            targets=targets,
            opened_at=t.to_pydatetime(),
            opened_bar=i,
        )
        rec = TradeRecord(
            symbol=plan.symbol,
            strategy=signal.strategy,
            side=plan.side,
            entry_time=t.to_pydatetime(),
            entry_price=entry,
            qty=qty,
            initial_stop=stop,
            fees=fee,
            pnl=-fee,
            score=signal.score,
        )
        return _Open(pos, rec)

    def _book_exit(self, op: _Open, f: ExitFill, t: pd.Timestamp | datetime, i: int) -> Decimal:
        long = op.pos.is_long
        px = self._slipped(f.price, buy=not long)
        gross = (px - op.pos.entry_price) * f.qty if long else (op.pos.entry_price - px) * f.qty
        fee = px * f.qty * self.fee_rate
        rec = op.record
        rec.exits.append(ExitFill(px, f.qty, f.reason))
        rec.pnl += gross - fee
        rec.fees += fee
        rec.exit_reason = f.reason
        rec.exit_time = pd.Timestamp(t).to_pydatetime()
        rec.bars_held = i - op.pos.opened_bar
        total = sum((x.qty for x in rec.exits), ZERO)
        rec.exit_price = sum((x.price * x.qty for x in rec.exits), ZERO) / total if total else None
        return gross - fee


def _bars_per_year(data: dict[str, pd.DataFrame]) -> float:
    for df in data.values():
        if len(df) >= 2:
            idx = pd.DatetimeIndex(df.index)
            naive = idx.tz_convert("UTC").tz_localize(None) if idx.tz is not None else idx
            ns = naive.to_numpy(dtype="datetime64[ns]").astype(np.int64)
            step = float(np.median(np.diff(ns)))
            if step > 0:
                return 365 * 24 * 3600 * 1e9 / step
    return 365 * 24.0
