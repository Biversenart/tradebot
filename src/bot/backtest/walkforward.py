"""Walk-forward optimisation and parameter sweeps (spec §5.9) with overfitting warnings.

For each fold: sweep the parameter grid on the TRAIN window, pick the best by the objective,
then run that single parameter set on the following unseen TEST window. Only the chained
TEST (out-of-sample) results count for the §9 thresholds.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from statistics import median
from typing import Any

import pandas as pd

from bot.backtest.engine import Backtester, BacktestResult, TradeRecord
from bot.backtest.metrics import Metrics, metrics_from
from bot.config.schema import AppConfig
from bot.strategies.registry import build_strategy

DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "ema_crossover": {"fast": [5, 9, 13], "slow": [21, 34, 55]},
    "rsi_reversion": {"buy_below": [25, 30, 35], "sell_above": [65, 70, 75]},
    "breakout": {"donchian": [20, 40, 55], "volume_mult": [1.2, 1.5, 2.0]},
    "grid": {"levels": [6, 10, 14], "range_bars": [100, 200]},
    "dca": {"interval_bars": [24, 72], "dip_pct": [0, 3, 6]},
}


def param_combinations(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not grid:
        return [{}]
    keys = sorted(grid)
    return [
        dict(zip(keys, vals, strict=True)) for vals in itertools.product(*(grid[k] for k in keys))
    ]


def objective_value(m: Metrics, objective: str, min_trades: int) -> float:
    if m.trades < min_trades:
        return -math.inf
    if objective == "profit_factor":
        return m.profit_factor if math.isfinite(m.profit_factor) else 10.0
    if objective == "sharpe":
        return m.sharpe
    return m.expectancy_r


def run_once(
    df: pd.DataFrame,
    symbol: str,
    strategy: str,
    params: dict[str, Any],
    cfg: AppConfig,
    trade_start: pd.Timestamp | None = None,
    trade_end: pd.Timestamp | None = None,
) -> tuple[BacktestResult, Metrics]:
    bt = Backtester(
        {symbol: df},
        lambda s: build_strategy(cfg, strategy, s, overrides=params),
        cfg,
        trade_start=trade_start,
        trade_end=trade_end,
    )
    res = bt.run()
    m = metrics_from(res.trades, res.equity, float(res.initial_equity), res.bars_per_year)
    return res, m


@dataclass
class SweepRow:
    params: dict[str, Any]
    metrics: Metrics
    objective: float


@dataclass
class Fold:
    index: int
    train_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    best_params: dict[str, Any]
    is_metrics: Metrics
    oos_metrics: Metrics
    oos: BacktestResult
    sweep: list[SweepRow]


@dataclass
class WalkForwardResult:
    symbol: str
    strategy: str
    folds: list[Fold]
    oos_trades: list[TradeRecord]
    oos_equity: pd.Series
    oos_metrics: Metrics
    is_metrics: Metrics | None
    warnings: list[str] = field(default_factory=list)
    initial_equity: float = 10_000.0

    def sweep_table(self) -> pd.DataFrame:
        rows: dict[str, list[float]] = {}
        for f in self.folds:
            for r in f.sweep:
                key = ", ".join(f"{k}={v}" for k, v in sorted(r.params.items())) or "(varsayılan)"
                rows.setdefault(key, []).append(r.objective)
        data = [
            {
                "params": k,
                "mean_objective": sum(v) / len(v)
                if all(math.isfinite(x) for x in v)
                else float("nan"),
                "folds": len(v),
            }
            for k, v in rows.items()
        ]
        return pd.DataFrame(data).sort_values("mean_objective", ascending=False, na_position="last")


def _slice(df: pd.DataFrame, start: int, end: int, warmup: int) -> pd.DataFrame:
    return df.iloc[max(0, start - 3 * warmup) : end]


def walk_forward(
    df: pd.DataFrame,
    symbol: str,
    strategy: str,
    cfg: AppConfig | None = None,
    grid: dict[str, list[Any]] | None = None,
) -> WalkForwardResult:
    cfg = cfg or AppConfig()
    wf = cfg.backtest.walk_forward
    grid = (
        grid
        if grid is not None
        else cfg.backtest.param_grids.get(strategy, DEFAULT_GRIDS.get(strategy, {}))
    )
    combos = param_combinations(grid)
    warmup = build_strategy(cfg, strategy, symbol).warmup_bars
    idx = df.index
    folds: list[Fold] = []
    k = 0
    start = warmup
    while start + wf.train_bars + wf.test_bars // 2 <= len(df):
        tr_s, tr_e = start, start + wf.train_bars
        te_e = min(tr_e + wf.test_bars, len(df))
        train_df = _slice(df, tr_s, tr_e, warmup)
        sweep: list[SweepRow] = []
        for params in combos:
            _, m = run_once(train_df, symbol, strategy, params, cfg, idx[tr_s], idx[tr_e - 1])
            sweep.append(SweepRow(params, m, objective_value(m, wf.objective, wf.min_trades)))
        best = max(sweep, key=lambda r: r.objective)
        test_df = _slice(df, tr_e, te_e, warmup)
        test_end = idx[te_e - 1] if te_e < len(df) else None
        oos, om = run_once(test_df, symbol, strategy, best.params, cfg, idx[tr_e], test_end)
        folds.append(
            Fold(k, idx[tr_s], idx[tr_e], idx[te_e - 1], best.params, best.metrics, om, oos, sweep)
        )
        k += 1
        start += wf.test_bars
    initial = float(cfg.backtest.initial_equity)
    trades: list[TradeRecord] = []
    pieces: list[pd.Series] = []
    level = initial
    for f in folds:
        eq = f.oos.equity
        if eq.empty:
            continue
        scaled = eq / initial * level
        pieces.append(scaled)
        level = float(scaled.iloc[-1])
        trades.extend(f.oos.trades)
    oos_equity = pd.concat(pieces) if pieces else pd.Series(dtype=float)
    oos_equity = oos_equity[~oos_equity.index.duplicated(keep="last")]
    bpy = folds[0].oos.bars_per_year if folds else 8760.0
    oos_m = metrics_from(trades, oos_equity, initial, bpy)
    is_m = _average([f.is_metrics for f in folds]) if folds else None
    result = WalkForwardResult(
        symbol, strategy, folds, trades, oos_equity, oos_m, is_m, initial_equity=initial
    )
    result.warnings = overfitting_warnings(result, wf.min_trades)
    return result


def _average(ms: list[Metrics]) -> Metrics:
    fields = Metrics.__dataclass_fields__
    vals: dict[str, Any] = {}
    for name in fields:
        xs = [getattr(m, name) for m in ms]
        finite = [x for x in xs if isinstance(x, int | float) and math.isfinite(x)]
        avg = sum(finite) / len(finite) if finite else 0.0
        vals[name] = round(avg) if name == "trades" else round(avg, 4)
    return Metrics(**vals)


def overfitting_warnings(r: WalkForwardResult, min_trades: int) -> list[str]:
    out: list[str] = []
    if not r.folds:
        return ["Walk-forward için yeterli veri yok (train + test penceresi sığmadı)."]
    oos, ins = r.oos_metrics, r.is_metrics
    if ins is not None:
        if ins.profit_factor > 0 and oos.profit_factor < 0.7 * ins.profit_factor:
            out.append(
                f"Örneklem dışı profit factor ({oos.profit_factor:.2f}) eğitimdekinin "
                f"({ins.profit_factor:.2f}) %70'inin altında: overfitting şüphesi."
            )
        if ins.expectancy > 0 >= oos.expectancy:
            out.append("Eğitimde pozitif olan beklenti örneklem dışında negatif: overfitting.")
    best_keys = {tuple(sorted(f.best_params.items())) for f in r.folds}
    if len(r.folds) >= 3 and len(best_keys) / len(r.folds) > 0.7:
        out.append("En iyi parametreler fold'dan fold'a sürekli değişiyor: parametre kararsızlığı.")
    for f in r.folds:
        objs = [x.objective for x in f.sweep if math.isfinite(x.objective)]
        if len(objs) >= 3:
            best, med = max(objs), median(objs)
            if med > 0 and best > 2 * med:
                out.append(
                    f"Fold {f.index}: en iyi kombinasyon medyanın 2 katından fazla — "
                    "tepe seçimi (peak picking) riski."
                )
                break
    if oos.trades < min_trades:
        out.append(
            f"Örneklem dışı işlem sayısı düşük ({oos.trades}); sonuçlar istatistiksel olarak zayıf."
        )
    return out
