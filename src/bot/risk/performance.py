"""Per strategy x symbol realised performance (spec §5.6 "strateji performans takibi")."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal(0)


@dataclass(frozen=True)
class ClosedTrade:
    strategy: str
    symbol: str
    pnl: Decimal
    r_multiple: Decimal


@dataclass(frozen=True)
class PerfStats:
    trades: int
    win_rate: Decimal  # 0..1
    avg_win_r: Decimal
    avg_loss_r: Decimal  # positive number
    expectancy_r: Decimal


class PerformanceTracker:
    def __init__(self, lookback: int = 30, history: int = 1000) -> None:
        self.lookback = lookback
        self._recent: dict[tuple[str, str], deque[ClosedTrade]] = defaultdict(
            lambda: deque(maxlen=lookback)
        )
        self._all: dict[str, deque[ClosedTrade]] = defaultdict(lambda: deque(maxlen=history))
        self.loss_streak = 0

    def record(self, t: ClosedTrade) -> None:
        self._recent[(t.strategy, t.symbol)].append(t)
        self._all[t.strategy].append(t)
        self.loss_streak = self.loss_streak + 1 if t.pnl < 0 else 0

    @staticmethod
    def _stats(trades: list[ClosedTrade]) -> PerfStats:
        n = len(trades)
        if n == 0:
            return PerfStats(0, ZERO, ZERO, ZERO, ZERO)
        wins = [t.r_multiple for t in trades if t.pnl > 0]
        losses = [-t.r_multiple for t in trades if t.pnl <= 0]
        avg_w = sum(wins, ZERO) / len(wins) if wins else ZERO
        avg_l = sum(losses, ZERO) / len(losses) if losses else ZERO
        return PerfStats(
            trades=n,
            win_rate=Decimal(len(wins)) / n,
            avg_win_r=avg_w,
            avg_loss_r=avg_l,
            expectancy_r=sum((t.r_multiple for t in trades), ZERO) / n,
        )

    def reset(self, strategy: str, symbol: str) -> None:
        self._recent.pop((strategy, symbol), None)

    def recent(self, strategy: str, symbol: str) -> PerfStats:
        return self._stats(list(self._recent[(strategy, symbol)]))

    def strategy(self, name: str) -> PerfStats:
        return self._stats(list(self._all[name]))
