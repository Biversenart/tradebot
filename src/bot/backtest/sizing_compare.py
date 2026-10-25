"""Fixed-fractional vs growth-oriented sizing on identical signals (spec §5.6, Prompt 5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from bot.backtest.engine import Backtester, FixedFractionalSizer, Sizer
from bot.backtest.metrics import Metrics, metrics_from
from bot.config.schema import AppConfig
from bot.risk.sizing import GrowthSizer
from bot.strategies.registry import build_strategy


@dataclass(frozen=True)
class SizingRow:
    symbol: str
    strategy: str
    fixed: Metrics
    growth: Metrics


def _run(df: pd.DataFrame, symbol: str, strategy: str, cfg: AppConfig, sizer: Sizer) -> Metrics:
    bt = Backtester({symbol: df}, lambda s: build_strategy(cfg, strategy, s), cfg, sizer=sizer)
    r = bt.run()
    return metrics_from(r.trades, r.equity, float(r.initial_equity), r.bars_per_year)


def compare_sizing(
    frames: dict[str, pd.DataFrame], strategies: list[str], cfg: AppConfig
) -> list[SizingRow]:
    rows = []
    risk = cfg.risk
    for symbol, df in frames.items():
        for name in strategies:
            fixed = FixedFractionalSizer(
                risk.risk_per_trade_pct, risk.max_exposure_per_symbol_pct * 5
            )
            growth = GrowthSizer(
                risk, cfg.backtest.initial_equity, risk.max_exposure_per_symbol_pct * 5
            )
            rows.append(
                SizingRow(
                    symbol,
                    name,
                    _run(df, symbol, name, cfg, fixed),
                    _run(df, symbol, name, cfg, growth),
                )
            )
    return rows


def sizing_markdown(rows: list[SizingRow], data_note: str) -> str:
    lines = [
        "# Boyutlama Karşılaştırması — Sabit %1 risk vs Büyüme odaklı",
        "",
        f"_{datetime.now(UTC):%Y-%m-%d %H:%M} UTC_ · {data_note}",
        "",
        "Aynı sinyaller, aynı komisyon/kayma; yalnızca pozisyon boyutu farklı. Örneklem içi "
        "(tam dönem) karşılaştırmadır; formüller `docs/KARARLAR.md`.",
        "",
        "| Sembol | Strateji | İşlem | Getiri % (sabit → büyüme) | Maks DD % | Sharpe | PF |",
        "|---|---|---:|---|---|---|---|",
    ]
    for r in rows:
        f, g = r.fixed, r.growth
        lines.append(
            f"| {r.symbol} | {r.strategy} | {f.trades}/{g.trades} | "
            f"{f.total_return_pct:.2f} → {g.total_return_pct:.2f} | "
            f"{f.max_drawdown_pct:.2f} → {g.max_drawdown_pct:.2f} | "
            f"{f.sharpe:.2f} → {g.sharpe:.2f} | {f.profit_factor:.2f} → {g.profit_factor:.2f} |"
        )
    better_dd = sum(1 for r in rows if r.growth.max_drawdown_pct <= r.fixed.max_drawdown_pct)
    lines += [
        "",
        f"Büyüme boyutlaması drawdown'u {better_dd}/{len(rows)} kombinasyonda azalttı "
        "veya eşit tuttu.",
        "",
    ]
    return "\n".join(lines)
