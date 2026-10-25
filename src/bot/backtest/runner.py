"""Programmatic entry points used by the CLI (`bot backtest ...`)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from bot.analysis.data import resample_ohlcv
from bot.backtest.bias import bias_checks
from bot.backtest.report import summary_markdown, write_report
from bot.backtest.walkforward import WalkForwardResult, walk_forward
from bot.config.schema import AppConfig
from bot.core.timeframes import timeframe_seconds
from bot.marketdata.history import load_ohlcv, parquet_path, safe_symbol
from bot.strategies.base import assert_causal
from bot.strategies.registry import STRATEGIES, build_strategy


class NoDataError(FileNotFoundError):
    pass


def load_history(
    data_root: Path,
    exchange: str,
    symbol: str,
    timeframe: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> pd.DataFrame:
    path = parquet_path(data_root, exchange, symbol, timeframe)
    if path.exists():
        df = load_ohlcv(path)
    else:
        lower = [
            tf
            for tf in ("1m", "5m", "15m", "1h", "4h")
            if timeframe_seconds(tf) < timeframe_seconds(timeframe)
            and timeframe_seconds(timeframe) % timeframe_seconds(tf) == 0
            and parquet_path(data_root, exchange, symbol, tf).exists()
        ]
        if not lower:
            raise NoDataError(
                f"{symbol} {timeframe} verisi yok ({path}). Önce: bot data download --symbol "
                f"{symbol} --tf {timeframe} --since 2022-01-01"
            )
        df = resample_ohlcv(
            load_ohlcv(parquet_path(data_root, exchange, symbol, lower[-1])), timeframe
        )
    if since is not None:
        df = df[df.index >= pd.Timestamp(since)]
    if until is not None:
        df = df[df.index < pd.Timestamp(until)]
    return df


def lookahead_check(
    cfg: AppConfig, strategy: str, symbol: str, df: pd.DataFrame, n: int = 5
) -> bool:
    """Spec §5.13: re-run the strategy on truncated history and compare (no look-ahead)."""
    s = build_strategy(cfg, strategy, symbol)
    sample = df.iloc[-min(len(df), 3000) :]
    lo = min(len(sample) - 1, s.warmup_bars + 10)
    if lo >= len(sample) - 1:
        return True
    rng = np.random.default_rng(7)
    points = sorted({int(x) for x in rng.integers(lo, len(sample), size=n)})
    try:
        assert_causal(s, sample, points)
    except AssertionError:
        return False
    return True


def run_report(
    cfg: AppConfig,
    frames: dict[str, pd.DataFrame],
    strategies: list[str],
    out_dir: Path,
    data_note: str,
    timeframe: str = "1h",
) -> tuple[Path, list[Path], list[WalkForwardResult]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[WalkForwardResult] = []
    html_paths: list[Path] = []
    look_ok = True
    for symbol, df in frames.items():
        for name in strategies:
            if name not in STRATEGIES:
                raise KeyError(f"Bilinmeyen strateji: {name}")
            ok = lookahead_check(cfg, name, symbol, df)
            look_ok &= ok
            wf = walk_forward(df, symbol, name, cfg)
            results.append(wf)
            checks = bias_checks(cfg.backtest, ok, [symbol], {symbol: df}, timeframe)
            html_paths.append(
                write_report(wf, checks, out_dir / f"{safe_symbol(symbol)}_{name}.html")
            )
    checks = bias_checks(cfg.backtest, look_ok, list(frames), frames, timeframe)
    summary = summary_markdown(results, checks, data_note)
    path = out_dir / "OZET.md"
    path.write_text(summary, encoding="utf-8")
    return path, html_paths, results


def default_out_dir(root: Path = Path("reports/backtest")) -> Path:
    return root / datetime.now(UTC).strftime("%Y%m%d-%H%M")
