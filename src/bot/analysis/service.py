"""Run a full symbol analysis and write the Markdown report + HTML chart (CLI, Telegram, panel)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bot.analysis.chart import build_figure, write_chart
from bot.analysis.data import load_frames
from bot.analysis.engine import analyze_symbol
from bot.analysis.plan import PlanResult, best_plan
from bot.analysis.report import render_report
from bot.config import Settings
from bot.config.schema import ExchangeConfig
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.factory import build_public_adapter
from bot.marketdata.history import safe_symbol
from bot.net.egress import resolve_egress


class AnalysisUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnalysisOutput:
    symbol: str
    report: str
    md_path: Path
    html_path: Path
    best: PlanResult | None

    def short_tr(self, max_lines: int = 12) -> str:
        lines = [ln for ln in self.report.splitlines() if ln.strip()]
        summary = lines[:max_lines]
        return "\n".join(summary)


async def run_analysis(
    settings: Settings,
    symbol: str,
    exchange: str = "binance",
    data_root: Path = Path("data"),
    out_dir: Path = Path("reports/analysis"),
    offline: bool = False,
    adapter: ExchangeAdapter | None = None,
) -> AnalysisOutput:
    cfg = settings.config.analysis
    tfs = [*cfg.timeframes.long, *cfg.timeframes.mid, *cfg.timeframes.short]
    own = False
    if adapter is None and not offline:
        ex_cfg = settings.config.exchanges.get(exchange) or ExchangeConfig(enabled=True)
        adapter = build_public_adapter(exchange, ex_cfg, resolve_egress(settings), testnet=False)
        own = True
    try:
        if adapter is not None and own:
            await adapter.load_markets()
        frames, sources = await load_frames(
            symbol,
            tfs,
            exchange=exchange,
            data_root=data_root,
            bars=cfg.analysis_bars,
            adapter=adapter,
        )
    finally:
        if adapter is not None and own:
            await adapter.close()
    if not frames:
        raise AnalysisUnavailableError(
            f"{symbol} için veri yok. Önce: bot data download --symbol {symbol} --since ..."
        )
    mtf = analyze_symbol(frames, symbol, exchange, cfg)
    setup_tf = mtf.setup_timeframe()
    if setup_tf is None:
        raise AnalysisUnavailableError("Analiz için yeterli mum yok.")
    best, results = best_plan(mtf, cfg)
    report = render_report(mtf, results, best, sources)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"{safe_symbol(symbol)}_{stamp}"
    md_path = base.with_suffix(".md")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(report, encoding="utf-8")
    shown = best or max(results, key=lambda r: r.score.total, default=None)
    fig = build_figure(mtf.frames[setup_tf], shown, f"{symbol} {setup_tf}", cfg.chart_bars)
    html_path = write_chart(fig, base.with_suffix(".html"))
    return AnalysisOutput(symbol, report, md_path, html_path, best)
