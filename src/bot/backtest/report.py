"""HTML / Markdown backtest reports (spec §5.9) incl. §5.13 bias checks and §9 comparison."""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from bot.backtest.engine import TradeRecord
from bot.backtest.metrics import Metrics, Thresholds, drawdown_series
from bot.backtest.walkforward import WalkForwardResult
from bot.config.schema import BacktestConfig

METRIC_LABELS = {
    "trades": "İşlem sayısı",
    "total_return_pct": "Toplam getiri %",
    "cagr_pct": "CAGR %",
    "sharpe": "Sharpe",
    "sortino": "Sortino",
    "max_drawdown_pct": "Maks. drawdown %",
    "win_rate_pct": "Kazanma oranı %",
    "profit_factor": "Profit factor",
    "expectancy": "Beklenti (işlem başı)",
    "expectancy_r": "Beklenti (R)",
    "avg_win": "Ort. kazanç",
    "avg_loss": "Ort. kayıp",
    "avg_bars_held": "Ort. tutma (mum)",
    "exposure_pct": "Piyasada kalma %",
    "final_equity": "Son bakiye",
}
CHECK_LABELS = {
    "profit_factor": "Profit factor ≥ 1.3",
    "expectancy": "Pozitif beklenti",
    "max_drawdown": "Maks. drawdown ≤ %15",
    "sample_size": "≥ 100 işlem",
}


@dataclass(frozen=True)
class BiasChecks:
    lookahead_passed: bool
    survivorship_note: str
    commission_pct: float
    slippage_bps: float
    latency_bars: int

    def lines(self) -> list[str]:
        return [
            f"Lookahead (geleceği görme) testi: {'GEÇTİ' if self.lookahead_passed else 'BAŞARISIZ'}",
            f"Survivorship: {self.survivorship_note}",
            f"Komisyon: %{self.commission_pct:g} / taraf · Kayma: {self.slippage_bps:g} bps · "
            f"Gecikme: {self.latency_bars} mum (sinyal kapanışta, dolum sonraki açılışta)",
        ]


def bias_checks(cfg: BacktestConfig, lookahead_passed: bool, symbols: list[str]) -> BiasChecks:
    note = (
        "Bu rapor yalnızca seçilen sembolleri içerir; delist olmuş coinler dahil değil. "
        "Sonuçlar hayatta kalan coinlere göre iyimser olabilir."
    )
    return BiasChecks(
        lookahead_passed, note, float(cfg.commission_pct), float(cfg.slippage_bps), cfg.latency_bars
    )


def _fmt(v: float | int) -> str:
    if isinstance(v, float) and not math.isfinite(v):
        return "∞"
    return f"{v:,.2f}" if isinstance(v, float) else str(v)


def metrics_table_html(cols: dict[str, Metrics | None]) -> str:
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    rows = []
    for key, label in METRIC_LABELS.items():
        cells = "".join(
            f"<td>{_fmt(getattr(m, key)) if m is not None else '-'}</td>" for m in cols.values()
        )
        rows.append(f"<tr><th>{label}</th>{cells}</tr>")
    return f"<table><tr><th></th>{head}</tr>{''.join(rows)}</table>"


def trades_table_html(trades: list[TradeRecord], limit: int = 300) -> str:
    rows = []
    for t in trades[-limit:]:
        rows.append(
            "<tr>"
            + "".join(
                f"<td>{html.escape(str(x))}</td>"
                for x in (
                    t.entry_time.strftime("%Y-%m-%d %H:%M"),
                    t.symbol,
                    t.side.value,
                    f"{t.entry_price:.6g}",
                    f"{t.exit_price:.6g}" if t.exit_price else "-",
                    f"{t.pnl:.2f}",
                    f"{t.r_multiple:.2f}",
                    t.exit_reason,
                    t.bars_held,
                )
            )
            + "</tr>"
        )
    head = "".join(
        f"<th>{h}</th>"
        for h in (
            "Giriş",
            "Sembol",
            "Yön",
            "Giriş fiyatı",
            "Çıkış (ort.)",
            "PnL",
            "R",
            "Çıkış nedeni",
            "Mum",
        )
    )
    note = f"<p>Son {limit} işlem gösteriliyor.</p>" if len(trades) > limit else ""
    return f"{note}<table><tr>{head}</tr>{''.join(rows)}</table>"


def equity_figure(wf: WalkForwardResult) -> go.Figure:
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.04
    )
    eq = wf.oos_equity
    fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name="Örneklem dışı equity"), row=1, col=1)
    dd = drawdown_series(eq) if not eq.empty else eq
    fig.add_trace(
        go.Scatter(
            x=dd.index, y=dd.values, name="Drawdown %", fill="tozeroy", line={"color": "#e74c3c"}
        ),
        row=2,
        col=1,
    )
    for f in wf.folds:
        fig.add_vline(x=f.test_start, line={"dash": "dot", "width": 1, "color": "#95a5a6"})
    fig.update_layout(
        template="plotly_white",
        height=600,
        title=f"{wf.symbol} · {wf.strategy} · walk-forward (yalnızca test pencereleri)",
    )
    return fig


CSS = """body{font-family:system-ui,sans-serif;margin:24px;max-width:1200px}
table{border-collapse:collapse;margin:12px 0}td,th{border:1px solid #ddd;padding:4px 8px;
font-size:13px;text-align:right}th{background:#f5f5f5}.warn{color:#c0392b}.ok{color:#27ae60}"""


def render_html(
    wf: WalkForwardResult, checks: BiasChecks, thresholds: Thresholds | None = None
) -> str:
    th = thresholds or Thresholds()
    res = th.check(wf.oos_metrics)
    verdict = "".join(
        f"<li class={'ok' if v else 'warn'}>{'✔' if v else '✘'} {CHECK_LABELS[k]}</li>"
        for k, v in res.items()
    )
    warnings = (
        "".join(f"<li class=warn>{html.escape(w)}</li>" for w in wf.warnings) or "<li>Yok</li>"
    )
    folds = "".join(
        f"<tr><td>{f.index}</td><td>{f.train_start:%Y-%m-%d}</td><td>{f.test_start:%Y-%m-%d}</td>"
        f"<td>{f.test_end:%Y-%m-%d}</td><td>{html.escape(str(f.best_params))}</td>"
        f"<td>{f.is_metrics.trades}</td><td>{_fmt(f.is_metrics.profit_factor)}</td>"
        f"<td>{f.oos_metrics.trades}</td><td>{_fmt(f.oos_metrics.profit_factor)}</td></tr>"
        for f in wf.folds
    )
    sweep = wf.sweep_table().head(30).to_html(index=False, float_format=lambda x: f"{x:.4f}")
    fig = equity_figure(wf).to_html(include_plotlyjs="cdn", full_html=False)
    bias = "".join(f"<li>{html.escape(x)}</li>" for x in checks.lines())
    return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<title>Backtest {html.escape(wf.symbol)} {html.escape(wf.strategy)}</title><style>{CSS}</style></head><body>
<h1>{html.escape(wf.symbol)} — {html.escape(wf.strategy)}</h1>
<p>Oluşturma: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC. Yalnızca örneklem dışı (walk-forward test)
sonuçlar §9 eşikleriyle karşılaştırılır. Geçmiş performans geleceği garanti etmez.</p>
<h2>§9 eşikleri (örneklem dışı)</h2><ul>{verdict}</ul>
<h2>Overfitting uyarıları</h2><ul>{warnings}</ul>
<h2>Önyargı kontrolleri (§5.13)</h2><ul>{bias}</ul>
<h2>Metrikler</h2>{metrics_table_html({"Eğitim (ort.)": wf.is_metrics, "Örneklem dışı": wf.oos_metrics})}
{fig}
<h2>Fold'lar</h2><table><tr><th>#</th><th>Eğitim başı</th><th>Test başı</th><th>Test sonu</th>
<th>En iyi parametre</th><th>IS işlem</th><th>IS PF</th><th>OOS işlem</th><th>OOS PF</th></tr>{folds}</table>
<h2>Parametre taraması (eğitim objektifi, fold ortalaması)</h2>{sweep}
<h2>Örneklem dışı işlemler</h2>{trades_table_html(wf.oos_trades)}
</body></html>"""


def write_report(wf: WalkForwardResult, checks: BiasChecks, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(wf, checks), encoding="utf-8")
    return path


def summary_markdown(
    results: list[WalkForwardResult],
    checks: BiasChecks,
    data_note: str,
    thresholds: Thresholds | None = None,
) -> str:
    th = thresholds or Thresholds()
    lines = [
        "# Backtest Özeti — Spec §9 Karşılaştırması",
        "",
        f"_{datetime.now(UTC):%Y-%m-%d %H:%M} UTC_",
        "",
        f"> {data_note}",
        "",
        "Yalnızca **walk-forward örneklem dışı** sonuçlar kullanılmıştır. Live'a geçiş için ayrıca "
        "en az 2 hafta paper sonuçlarının da eşikleri sağlaması gerekir.",
        "",
        "| Sembol | Strateji | İşlem | PF | Beklenti | Beklenti (R) | Maks DD % | Kazanma % | Sharpe | §9 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    passed = 0
    for r in results:
        m = r.oos_metrics
        ok = th.passes(m)
        passed += ok
        lines.append(
            f"| {r.symbol} | {r.strategy} | {m.trades} | {_fmt(m.profit_factor)} | "
            f"{m.expectancy:.2f} | {m.expectancy_r:.3f} | {m.max_drawdown_pct:.2f} | "
            f"{m.win_rate_pct:.1f} | {m.sharpe:.2f} | {'✅' if ok else '❌'} |"
        )
    lines += [
        "",
        f"**Eşikleri geçen kombinasyon: {passed}/{len(results)}**",
        "",
        "## Overfitting uyarıları",
    ]
    any_warn = False
    for r in results:
        for w in r.warnings:
            any_warn = True
            lines.append(f"- {r.symbol} / {r.strategy}: {w}")
    if not any_warn:
        lines.append("- Yok")
    lines += ["", "## Önyargı kontrolleri", *[f"- {x}" for x in checks.lines()], ""]
    lines += [
        "## Değerlendirme",
        "- Kazanma oranı tek başına kriter değildir; profit factor, beklenti ve drawdown birlikte "
        "değerlendirilir.",
        "- Eşikleri geçmeyen kombinasyonlar live için önerilmez.",
        "- Eşikleri geçenler bile önce ≥2 hafta paper/testnet'te doğrulanmalıdır (spec §9).",
    ]
    return "\n".join(lines) + "\n"
