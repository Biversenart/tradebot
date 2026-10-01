"""Backtest metrics (spec §5.9 / §9). Ratios use floats; PnL sums use Decimal."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal

import numpy as np
import pandas as pd

from bot.backtest.engine import BacktestResult, TradeRecord


@dataclass(frozen=True)
class Metrics:
    trades: int
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    expectancy: float  # average net PnL per trade (quote currency)
    expectancy_r: float  # average R multiple
    avg_win: float
    avg_loss: float
    avg_bars_held: float
    exposure_pct: float
    final_equity: float

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


def max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return max(0.0, float(-dd.min() * 100))


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return (equity - peak) / peak * 100


def profit_factor(pnls: list[Decimal]) -> float:
    gains = sum((p for p in pnls if p > 0), Decimal(0))
    losses = -sum((p for p in pnls if p < 0), Decimal(0))
    if losses == 0:
        return math.inf if gains > 0 else 0.0
    return float(gains / losses)


def compute_metrics(result: BacktestResult) -> Metrics:
    return metrics_from(
        result.trades, result.equity, float(result.initial_equity), result.bars_per_year
    )


def metrics_from(
    trades: list[TradeRecord], equity: pd.Series, initial: float, bars_per_year: float
) -> Metrics:
    pnls = [t.pnl for t in trades]
    n = len(trades)
    final = float(equity.iloc[-1]) if not equity.empty else initial
    total_ret = (final / initial - 1) * 100 if initial else 0.0
    years = len(equity) / bars_per_year if bars_per_year > 0 else 0.0
    cagr = ((final / initial) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else 0.0
    rets = equity.pct_change().dropna()
    std = float(rets.std(ddof=0)) if len(rets) > 1 else 0.0
    mean = float(rets.mean()) if len(rets) else 0.0
    ann = math.sqrt(bars_per_year)
    sharpe = mean / std * ann if std > 0 else 0.0
    downside = rets[rets < 0]
    dstd = float(np.sqrt((downside**2).mean())) if len(downside) else 0.0
    sortino = mean / dstd * ann if dstd > 0 else 0.0
    wins = [float(p) for p in pnls if p > 0]
    losses = [float(p) for p in pnls if p <= 0]
    bars_in = sum(t.bars_held for t in trades)
    return Metrics(
        trades=n,
        total_return_pct=round(total_ret, 4),
        cagr_pct=round(cagr, 4),
        sharpe=round(sharpe, 4),
        sortino=round(sortino, 4),
        max_drawdown_pct=round(max_drawdown_pct(equity), 4),
        win_rate_pct=round(100 * len(wins) / n, 2) if n else 0.0,
        profit_factor=round(profit_factor(pnls), 4) if n else 0.0,
        expectancy=round(float(sum(pnls, Decimal(0)) / n), 4) if n else 0.0,
        expectancy_r=round(float(sum((t.r_multiple for t in trades), Decimal(0)) / n), 4)
        if n
        else 0.0,
        avg_win=round(float(np.mean(wins)), 4) if wins else 0.0,
        avg_loss=round(float(np.mean(losses)), 4) if losses else 0.0,
        avg_bars_held=round(bars_in / n, 2) if n else 0.0,
        exposure_pct=round(100 * min(1.0, bars_in / len(equity)), 2) if len(equity) else 0.0,
        final_equity=round(final, 2),
    )


@dataclass(frozen=True)
class Thresholds:
    """Spec §9: live is only recommended when ALL hold on out-of-sample results."""

    min_profit_factor: float = 1.3
    max_drawdown_pct: float = 15.0
    min_trades: int = 100

    def check(self, m: Metrics) -> dict[str, bool]:
        return {
            "profit_factor": m.profit_factor >= self.min_profit_factor,
            "expectancy": m.expectancy > 0,
            "max_drawdown": m.max_drawdown_pct <= self.max_drawdown_pct,
            "sample_size": m.trades >= self.min_trades,
        }

    def passes(self, m: Metrics) -> bool:
        return all(self.check(m).values())
