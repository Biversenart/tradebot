"""Exit management shared by live trading and backtests (spec §5.3.i, §5.4).

Per bar (long; short mirrored), in this order:
1. Gap through the stop at the open -> exit everything at the open.
2. Stop touched intrabar -> exit everything at the stop (assumed to happen BEFORE any target:
   conservative when a bar touches both).
3. Targets: TP1 closes `tp1_close_pct` of the initial size and (optionally) moves the stop to
   break-even; intermediate targets close half of the remainder; the last target closes all.
4. After TP1 the stop trails (ATR: best close - mult x ATR; structure: lowest low of the last
   N bars). Trailing only tightens and takes effect from the next bar.
5. Time exit: if no target was reached after `time_exit_bars`, exit at the close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from bot.core.models import PositionSide

ZERO = Decimal(0)


@dataclass(frozen=True)
class ExitRules:
    tp1_close_pct: Decimal = Decimal(40)
    move_sl_to_breakeven_after_tp1: bool = True
    trailing: Literal["atr", "structure", "none"] = "atr"
    trailing_atr_mult: Decimal = Decimal(2)
    structure_trail_bars: int = 10
    time_exit_bars: int = 48


@dataclass
class ExitFill:
    price: Decimal
    qty: Decimal
    reason: str  # stop | stop_gap | tp1 | tp2 | tp3 | trailing_stop | breakeven | time


@dataclass
class ManagedPosition:
    symbol: str
    strategy: str
    side: PositionSide
    entry_price: Decimal
    initial_qty: Decimal
    initial_stop: Decimal
    stop: Decimal
    targets: list[Decimal]
    opened_at: datetime
    opened_bar: int
    qty: Decimal = ZERO
    targets_hit: int = 0
    best_price: Decimal = ZERO
    stop_reason: str = "stop"
    fills: list[ExitFill] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.qty == 0:
            self.qty = self.initial_qty
        if self.best_price == 0:
            self.best_price = self.entry_price

    @property
    def is_long(self) -> bool:
        return self.side is PositionSide.LONG

    @property
    def is_open(self) -> bool:
        return self.qty > 0

    @property
    def risk_per_unit(self) -> Decimal:
        return abs(self.entry_price - self.initial_stop)

    def unrealized(self, price: Decimal) -> Decimal:
        d = price - self.entry_price
        return (d if self.is_long else -d) * self.qty


def _close(pos: ManagedPosition, price: Decimal, qty: Decimal, reason: str) -> ExitFill:
    qty = min(qty, pos.qty)
    pos.qty -= qty
    f = ExitFill(price, qty, reason)
    pos.fills.append(f)
    return f


def on_bar(
    pos: ManagedPosition,
    rules: ExitRules,
    bar_index: int,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    atr: Decimal | None,
    trail_level: Decimal | None,
) -> list[ExitFill]:
    """Apply one bar to an open position; returns the exit fills (before slippage/fees)."""
    out: list[ExitFill] = []
    if not pos.is_open:
        return out
    long = pos.is_long
    # 1-2) stop first (gap at open, then intrabar)
    if (long and open_ <= pos.stop) or (not long and open_ >= pos.stop):
        out.append(_close(pos, open_, pos.qty, pos.stop_reason + "_gap"))
        return out
    if (long and low <= pos.stop) or (not long and high >= pos.stop):
        out.append(_close(pos, pos.stop, pos.qty, pos.stop_reason))
        return out
    # 3) targets
    while pos.targets_hit < len(pos.targets) and pos.is_open:
        tp = pos.targets[pos.targets_hit]
        if not ((long and high >= tp) or (not long and low <= tp)):
            break
        pos.targets_hit += 1
        last = pos.targets_hit == len(pos.targets)
        if last:
            qty = pos.qty
        elif pos.targets_hit == 1:
            qty = (pos.initial_qty * rules.tp1_close_pct / 100).min(pos.qty)
        else:
            qty = pos.qty / 2
        out.append(_close(pos, tp, qty, f"tp{pos.targets_hit}"))
        if pos.targets_hit == 1 and rules.move_sl_to_breakeven_after_tp1:
            better = pos.entry_price > pos.stop if long else pos.entry_price < pos.stop
            if better:
                pos.stop = pos.entry_price
                pos.stop_reason = "breakeven"
    if not pos.is_open:
        return out
    # 4) trailing after TP1 (effective next bar)
    pos.best_price = max(pos.best_price, close) if long else min(pos.best_price, close)
    if pos.targets_hit >= 1 and rules.trailing != "none":
        cand: Decimal | None = None
        if rules.trailing == "atr" and atr is not None and atr > 0:
            m = rules.trailing_atr_mult * atr
            cand = pos.best_price - m if long else pos.best_price + m
        elif rules.trailing == "structure" and trail_level is not None:
            cand = trail_level
        if cand is not None and ((long and cand > pos.stop) or (not long and cand < pos.stop)):
            pos.stop = cand
            pos.stop_reason = "trailing_stop"
    # 5) time exit
    if pos.targets_hit == 0 and bar_index - pos.opened_bar >= rules.time_exit_bars:
        out.append(_close(pos, close, pos.qty, "time"))
    return out
