"""Pure helpers that turn stored rows into panel views (stats, PnL, drawdown, markers)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from bot.storage.models import EquityPointRow, OrderRow, PositionRow

ZERO = Decimal(0)


def iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def r_multiple(p: PositionRow, pnl: Decimal) -> Decimal | None:
    risk = abs(p.entry_price - p.initial_stop) * p.initial_amount
    return pnl / risk if risk > 0 else None


def unrealized(p: PositionRow, mark: Decimal) -> Decimal:
    sign = 1 if p.side == "long" else -1
    return (mark - p.entry_price) * p.amount * sign


def position_view(p: PositionRow, mark: Decimal | None) -> dict[str, Any]:
    m = mark if mark is not None else p.entry_price
    upnl = unrealized(p, m)
    r = r_multiple(p, upnl + p.realized_pnl)
    return {
        "id": p.position_id,
        "exchange": p.exchange,
        "symbol": p.symbol,
        "side": p.side,
        "strategy": p.strategy,
        "timeframe": p.timeframe,
        "amount": str(p.amount),
        "initial_amount": str(p.initial_amount),
        "entry": str(p.entry_price),
        "mark": str(m),
        "mark_live": mark is not None,
        "stop": str(p.stop_loss),
        "stop_on_exchange": p.stop_order_id is not None,
        "targets": p.take_profits,
        "targets_hit": p.targets_hit,
        "notional": str(p.amount * m),
        "unrealized_pnl": str(upnl),
        "realized_pnl": str(p.realized_pnl),
        "r": None if r is None else f"{r:.2f}",
        "opened_at": iso(p.opened_at),
    }


def trade_view(p: PositionRow) -> dict[str, Any]:
    r = r_multiple(p, p.realized_pnl)
    return {
        "id": p.position_id,
        "exchange": p.exchange,
        "symbol": p.symbol,
        "side": p.side,
        "strategy": p.strategy,
        "entry": str(p.entry_price),
        "amount": str(p.initial_amount),
        "pnl": str(p.realized_pnl),
        "fees": str(p.fees),
        "r": None if r is None else f"{r:.2f}",
        "reason": p.close_reason,
        "opened_at": iso(p.opened_at),
        "closed_at": iso(p.closed_at),
    }


@dataclass
class Stats:
    trades: int = 0
    wins: int = 0
    pnl: Decimal = ZERO
    gross_win: Decimal = ZERO
    gross_loss: Decimal = ZERO
    r_sum: Decimal = ZERO
    r_count: int = 0

    def add(self, p: PositionRow) -> None:
        self.trades += 1
        self.pnl += p.realized_pnl
        if p.realized_pnl > 0:
            self.wins += 1
            self.gross_win += p.realized_pnl
        else:
            self.gross_loss += -p.realized_pnl
        r = r_multiple(p, p.realized_pnl)
        if r is not None:
            self.r_sum += r
            self.r_count += 1

    def to_dict(self) -> dict[str, Any]:
        pf = self.gross_win / self.gross_loss if self.gross_loss > 0 else None
        return {
            "trades": self.trades,
            "win_rate": round(self.wins / self.trades * 100, 1) if self.trades else None,
            "pnl": str(self.pnl),
            "profit_factor": None if pf is None else f"{pf:.2f}",
            "expectancy_r": f"{self.r_sum / self.r_count:.2f}" if self.r_count else None,
        }


def strategy_stats(rows: list[PositionRow]) -> dict[str, dict[str, Any]]:
    by: dict[str, Stats] = defaultdict(Stats)
    for p in rows:
        by[p.strategy].add(p)
    return {k: v.to_dict() for k, v in sorted(by.items())}


def total_stats(rows: list[PositionRow]) -> dict[str, Any]:
    s = Stats()
    for p in rows:
        s.add(p)
    return s.to_dict()


def daily_pnl(rows: list[PositionRow]) -> list[dict[str, str]]:
    by: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in rows:
        if p.closed_at is not None:
            by[p.closed_at.date().isoformat()] += p.realized_pnl
    return [{"day": d, "pnl": str(v)} for d, v in sorted(by.items())]


def drawdown(points: list[EquityPointRow]) -> tuple[list[dict[str, str]], Decimal]:
    """Equity curve with running drawdown %; returns (series, max drawdown %)."""
    out: list[dict[str, str]] = []
    peak, worst = ZERO, ZERO
    for e in points:
        peak = max(peak, e.equity)
        dd = (peak - e.equity) / peak * 100 if peak > 0 else ZERO
        worst = max(worst, dd)
        out.append({"t": e.timestamp.isoformat(), "equity": str(e.equity), "dd": f"{dd:.3f}"})
    return out, worst


def markers(positions: list[PositionRow], orders: list[OrderRow]) -> list[dict[str, Any]]:
    """Entry / exit points for the price chart."""
    out: list[dict[str, Any]] = []
    by_pos: dict[str, list[OrderRow]] = defaultdict(list)
    for o in orders:
        if o.position_id:
            by_pos[o.position_id].append(o)
    for p in positions:
        out.append(
            {
                "t": iso(p.opened_at),
                "price": str(p.entry_price),
                "kind": "entry",
                "side": p.side,
                "label": f"{p.strategy} giriş",
            }
        )
        for o in by_pos.get(p.position_id, []):
            if o.role == "entry" or o.average_price is None:
                continue
            out.append(
                {
                    "t": iso(o.updated_at or o.created_at),
                    "price": str(o.average_price),
                    "kind": "exit",
                    "side": p.side,
                    "label": f"{o.role} ({p.close_reason or 'kısmi'})",
                }
            )
    return out
