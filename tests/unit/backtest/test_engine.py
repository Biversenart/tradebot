"""Backtest engine arithmetic with a scripted strategy (known signals -> known PnL)."""

from __future__ import annotations

from decimal import Decimal as D
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import Backtester, BacktestResult, FixedFractionalSizer
from bot.backtest.metrics import compute_metrics
from bot.config.schema import AppConfig
from bot.strategies.base import BaseStrategy
from tests.fixtures.loader import frame


class Scripted(BaseStrategy):
    """Signals at fixed bars with a fixed stop distance."""

    name = "scripted"
    plan: ClassVar[dict[int, tuple[int, float]]] = {}

    @property
    def warmup_bars(self) -> int:
        return 0

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self.empty_frame(df)
        out["atr"] = 1.0
        out["score"] = 80.0
        for i, (direction, stop) in self.plan.items():
            if i < len(df):
                ts = out.index[i]
                out.loc[ts, "signal"] = direction
                out.loc[ts, "stop"] = stop
                out.loc[ts, "reason"] = "test"
        return out


def cfg(**bt: object) -> AppConfig:
    return AppConfig.model_validate(
        {
            "backtest": {"commission_pct": 0, "slippage_bps": 0, **bt},
            "analysis": {
                "trade_plan": {"take_profits": [1, 2, 3], "time_exit_bars": 100, "trailing": "none"}
            },
        }
    )


def flat_then(prices: list[float]) -> pd.DataFrame:
    o = [prices[0], *prices[:-1]]
    return frame(
        o,
        [max(a, b) + 0.1 for a, b in zip(o, prices, strict=True)],
        [min(a, b) - 0.1 for a, b in zip(o, prices, strict=True)],
        prices,
    )


def run(
    df: pd.DataFrame, plan: dict[int, tuple[int, float]], c: AppConfig | None = None
) -> BacktestResult:
    Scripted.plan = plan
    bt = Backtester(
        {"X/USDT": df}, lambda s: Scripted(s), c or cfg(), sizer=FixedFractionalSizer(D(1), D(1000))
    )
    return bt.run()


def test_entry_next_open_and_stop_loss_pnl() -> None:
    prices = [100.0] * 5 + [100, 99, 98, 97, 96, 95, 94]
    df = flat_then(prices)
    r = run(df, {3: (1, 97.0)})
    assert len(r.trades) == 1
    t = r.trades[0]
    assert t.entry_time == df.index[4].to_pydatetime()  # latency 1: next bar open
    assert t.entry_price == D("100.0")
    assert t.exit_reason == "stop"
    # risk 1% of 10000 = 100 over 3 points -> 33.33 units; loss = 100
    assert float(t.pnl) == pytest.approx(-100.0, rel=1e-6)
    assert float(t.r_multiple) == pytest.approx(-1.0)
    assert float(r.final_equity) == pytest.approx(9900.0)


def test_fees_and_slippage() -> None:
    prices = [100.0] * 5 + [100, 99, 98, 97, 96, 95, 94]
    r = run(flat_then(prices), {3: (1, 97.0)}, cfg(commission_pct=0.1, slippage_bps=10))
    t = r.trades[0]
    assert t.entry_price == D("100.0") * D("1.001")
    assert t.fees > 0
    assert float(t.pnl) < -100.0  # worse than frictionless


def test_targets_and_equity_conservation() -> None:
    prices = [100.0] * 5 + [100, 101, 103, 104, 106, 107, 110, 111]
    df = flat_then(prices)
    r = run(df, {3: (1, 98.0)})
    t = r.trades[0]
    assert [e.reason for e in t.exits] == ["tp1", "tp2", "tp3"]
    total = sum((x.pnl for x in r.trades), D(0))
    assert float(r.final_equity) == pytest.approx(10000 + float(total))
    m = compute_metrics(r)
    assert m.trades == 1 and m.win_rate_pct == 100 and m.profit_factor == float("inf")


def test_short_and_short_disabled() -> None:
    prices = [100.0] * 5 + [100, 99, 97, 96, 94, 93, 90, 88]
    df = flat_then(prices)
    r = run(df, {3: (-1, 102.0)})
    assert r.trades and r.trades[0].pnl > 0
    r2 = run(df, {3: (-1, 102.0)}, cfg(allow_short=False))
    assert r2.trades == []


def test_gap_through_stop_before_entry_skips_trade() -> None:
    df = frame(
        [100, 100, 100, 100, 90, 90],
        [100.1] * 4 + [91, 91],
        [99.9] * 4 + [89, 89],
        [100, 100, 100, 100, 90, 90],
    )
    assert run(df, {3: (1, 95.0)}).trades == []


def test_max_positions_and_trade_window() -> None:
    df = flat_then([100.0] * 40)
    r = run(df, {3: (1, 95.0), 4: (1, 95.0), 5: (1, 95.0)})
    assert len(r.trades) == 1  # max_positions=1 for the strategy
    Scripted.plan = {3: (1, 95.0), 20: (1, 95.0)}
    bt = Backtester({"X/USDT": df}, lambda s: Scripted(s), cfg(), trade_start=df.index[10])
    res = bt.run()
    assert [t.entry_time for t in res.trades] == [df.index[21].to_pydatetime()]


def test_no_signals_flat_equity() -> None:
    r = run(flat_then([100.0] * 20), {})
    assert r.trades == [] and np.allclose(r.equity.to_numpy(), 10000)


def test_time_exit() -> None:
    c = AppConfig.model_validate(
        {
            "backtest": {"commission_pct": 0, "slippage_bps": 0},
            "analysis": {"trade_plan": {"time_exit_bars": 3, "trailing": "none"}},
        }
    )
    r = run(flat_then([100.0] * 20), {2: (1, 95.0)}, c)
    assert r.trades[0].exit_reason == "time" and r.trades[0].bars_held == 3


def test_open_position_closed_at_end_of_data() -> None:
    r = run(flat_then([100.0] * 8), {2: (1, 95.0)})
    assert r.trades[0].exit_reason == "end_of_data"
