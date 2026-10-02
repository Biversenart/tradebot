from __future__ import annotations

import math
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import pytest

from bot.backtest.bias import bias_checks
from bot.backtest.metrics import Metrics, Thresholds, max_drawdown_pct, metrics_from, profit_factor
from bot.backtest.report import render_html, summary_markdown
from bot.backtest.runner import NoDataError, load_history, lookahead_check
from bot.backtest.walkforward import objective_value, param_combinations, walk_forward
from bot.config.schema import AppConfig
from bot.marketdata.history import parquet_path
from bot.strategies.base import BaseStrategy
from bot.strategies.registry import STRATEGIES
from tests.fixtures.loader import synthetic_1h

DF = synthetic_1h()
SMALL = AppConfig.model_validate(
    {"backtest": {"walk_forward": {"train_bars": 400, "test_bars": 200, "min_trades": 1}}}
)


def test_max_drawdown_and_pf() -> None:
    eq = pd.Series([100, 120, 90, 130, 104])
    assert max_drawdown_pct(eq) == pytest.approx(25.0)
    assert profit_factor([D(10), D(-5), D(20), D(-5)]) == pytest.approx(3.0)
    assert profit_factor([D(1)]) == math.inf
    assert profit_factor([]) == 0.0


def test_metrics_empty_and_sharpe() -> None:
    eq = pd.Series([100.0] * 10, index=pd.date_range("2024", periods=10, freq="1h", tz="UTC"))
    m = metrics_from([], eq, 100.0, 8760)
    assert m.trades == 0 and m.sharpe == 0 and m.max_drawdown_pct == 0 and m.total_return_pct == 0


def thresholds_metrics(**over: float) -> Metrics:
    base = dict(
        trades=150,
        total_return_pct=10.0,
        cagr_pct=5.0,
        sharpe=1.0,
        sortino=1.0,
        max_drawdown_pct=10.0,
        win_rate_pct=45.0,
        profit_factor=1.5,
        expectancy=3.0,
        expectancy_r=0.2,
        avg_win=10.0,
        avg_loss=-5.0,
        avg_bars_held=10.0,
        exposure_pct=30.0,
        final_equity=11000.0,
    )
    base.update(over)
    return Metrics(**base)  # type: ignore[arg-type]


def test_section9_thresholds() -> None:
    th = Thresholds()
    assert th.passes(thresholds_metrics())
    assert not th.passes(thresholds_metrics(profit_factor=1.2))
    assert not th.passes(thresholds_metrics(max_drawdown_pct=16))
    assert not th.passes(thresholds_metrics(trades=99))
    assert not th.passes(thresholds_metrics(expectancy=-0.1))


def test_param_combinations_and_objective() -> None:
    combos = param_combinations({"b": [1, 2], "a": [3]})
    assert combos == [{"a": 3, "b": 1}, {"a": 3, "b": 2}]
    assert param_combinations({}) == [{}]
    m = thresholds_metrics(trades=5)
    assert objective_value(m, "expectancy", 10) == -math.inf
    assert objective_value(m, "profit_factor", 1) == 1.5


def test_walk_forward_folds_are_out_of_sample() -> None:
    wf = walk_forward(DF, "BTC/USDT", "breakout", SMALL, grid={"donchian": [20, 40]})
    assert len(wf.folds) >= 3
    for f in wf.folds:
        assert f.train_start < f.test_start <= f.test_end
        for t in f.oos.trades:
            assert t.entry_time >= f.test_start.to_pydatetime()  # trades only in the test window
    starts = [f.test_start for f in wf.folds]
    assert starts == sorted(starts)
    assert wf.oos_metrics.trades == len(wf.oos_trades)
    assert not wf.oos_equity.empty and wf.oos_equity.index.is_monotonic_increasing
    table = wf.sweep_table()
    assert set(table.columns) == {"params", "mean_objective", "folds"}


def test_walk_forward_without_enough_data() -> None:
    wf = walk_forward(DF.iloc[:300], "BTC/USDT", "breakout", SMALL)
    assert wf.folds == [] and "yeterli veri yok" in wf.warnings[0]


def test_reports_render(tmp_path: Path) -> None:
    wf = walk_forward(DF, "BTC/USDT", "dca", SMALL, grid={"interval_bars": [24, 48]})
    checks = bias_checks(SMALL.backtest, True, ["BTC/USDT"])
    page = render_html(wf, checks)
    for part in (
        "§9 eşikleri",
        "Overfitting",
        "Önyargı kontrolleri",
        "Lookahead",
        "Fold'lar",
        "Parametre taraması",
        "plotly",
    ):
        assert part in page
    md = summary_markdown([wf], checks, "Sentetik veri.")
    assert "| BTC/USDT | dca |" in md and "Eşikleri geçen kombinasyon" in md


class Cheater(BaseStrategy):
    name = "cheater"

    @property
    def warmup_bars(self) -> int:
        return 0

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self.empty_frame(df)
        out["signal"] = (df["close"].shift(-1) > df["close"]).astype(int)  # peeks at the future
        out["stop"] = df["close"] * 0.9
        out["atr"] = 1.0
        return out


def test_lookahead_check_detects_cheating(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in STRATEGIES:
        assert lookahead_check(AppConfig(), name, "BTC/USDT", DF)
    monkeypatch.setitem(STRATEGIES, "cheater", Cheater)
    assert not lookahead_check(AppConfig(), "cheater", "BTC/USDT", DF)


def test_load_history(tmp_path: Path) -> None:
    p = parquet_path(tmp_path, "binance", "BTC/USDT", "1h")
    p.parent.mkdir(parents=True)
    DF.to_parquet(p)
    h = load_history(tmp_path, "binance", "BTC/USDT", "1h", since=DF.index[100].to_pydatetime())
    assert h.index[0] == DF.index[100]
    h4 = load_history(tmp_path, "binance", "BTC/USDT", "4h")
    assert len(h4) == len(DF) // 4
    with pytest.raises(NoDataError):
        load_history(tmp_path, "binance", "ETH/USDT", "1h")
