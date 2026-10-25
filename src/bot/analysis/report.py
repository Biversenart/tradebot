"""Readable Turkish analysis report (Markdown) for one symbol (spec §5.3.j)."""

from __future__ import annotations

from datetime import UTC, datetime

from bot.analysis.engine import MultiTimeframeAnalysis, TimeframeAnalysis
from bot.analysis.plan import PlanResult

GROUP_TR = {"long": "Uzun vade", "mid": "Orta vade", "short": "Kısa vade"}


def _f(x: float | None, digits: int = 2) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def _p(x: float | None) -> str:
    return "-" if x is None else f"{x:.6g}"


def timeframe_section(ta: TimeframeAnalysis) -> list[str]:
    ind = ta.ind
    lines = [f"#### {ta.timeframe}"]
    ema_txt = ", ".join(f"EMA{n} {_p(v)}" for n, v in sorted(ind.emas.items()))
    lines.append(
        f"- Fiyat **{_p(ind.close)}** · eğilim **{ta.bias.label_tr}** · rejim "
        f"{ta.regime.regime.label_tr}{' (sıkışma)' if ta.regime.squeeze else ''}"
    )
    lines.append(
        f"- RSI {_f(ind.rsi, 1)} · MACD hist {_f(ind.macd_hist, 4)} · ADX {_f(ind.adx, 1)} · "
        f"ATR {_p(ind.atr)} · hacim/ort. {_f(ind.volume_ratio)}"
    )
    if ema_txt:
        lines.append(f"- {ema_txt}")
    st = ta.structure
    ev = st.last_event
    ev_txt = f"; son olay {ev.kind} {ev.direction.label_tr} @ {_p(ev.level)}" if ev else ""
    labels = " ".join(s.label for s in st.swings[-6:] if s.label)
    lines.append(f"- Yapı: {st.trend.label_tr} ({labels or 'etiket yok'}){ev_txt}")
    price = ind.close
    zones = sorted(ta.zones, key=lambda z: z.distance(price))[:4]
    if zones:
        lines.append(
            "- Bölgeler: "
            + "; ".join(
                f"{z.kind.label_tr} {_p(z.low)}–{_p(z.high)} (güç {z.strength:.0f})" for z in zones
            )
        )
    live_pools = [p for p in ta.liquidity if not p.swept][:3]
    if live_pools:
        lines.append(
            "- Likidite: "
            + "; ".join(
                f"{'eşit tepeler' if p.side.value == 'buy_side' else 'eşit dipler'} {_p(p.price)}"
                for p in live_pools
            )
        )
    if ta.fib:
        fib = ta.fib
        lines.append(
            f"- Fibonacci ({'yükseliş' if fib.direction == 'up' else 'düşüş'} bacağı): "
            + ", ".join(f"{k:g}: {_p(v)}" for k, v in fib.retracements.items())
        )
    if ta.profile:
        vp = ta.profile
        lines.append(f"- Hacim profili: POC {_p(vp.poc)}, VAH {_p(vp.vah)}, VAL {_p(vp.val)}")
    divs = ta.recent_divergences(15)
    if divs:
        lines.append(
            "- Uyumsuzluk: " + ", ".join(f"{d.indicator.upper()} {d.kind.label_tr}" for d in divs)
        )
    cps = ta.recent_candles(3)
    charts = ta.charts
    if cps or charts:
        items = [f"{c.label_tr}{' (bölgede)' if c.at_zone else ''}" for c in cps]
        items += [f"{c.label_tr}{' ✔' if c.confirmed else ''}" for c in charts]
        lines.append("- Formasyonlar: " + ", ".join(dict.fromkeys(items)))
    return lines


def plan_section(r: PlanResult) -> list[str]:
    yon = "LONG" if r.direction.value == "bullish" else "SHORT"
    lines = [f"#### {yon} ({r.timeframe}) — confluence {r.score.total:.1f}/100"]
    for c in r.score.components:
        lines.append(f"  - {c.name}: {c.score * 100:.0f}% × {c.weight:g} — {c.reason}")
    if r.score.penalty < 1:
        lines.append(f"  - Üst zaman dilimi cezası: ×{r.score.penalty:g}")
    if r.plan is None:
        lines.append(f"- **Plan yok:** {r.rejected}")
        if r.entry_low is not None and r.stop is not None:
            lines.append(
                f"- (taslak) giriş {_p(r.entry_low)}–{_p(r.entry_high)}, stop {_p(r.stop)}, "
                f"hedefler {', '.join(_p(t) for t in r.targets)}"
            )
        return lines
    p = r.plan
    rrs = ", ".join(f"{float(x):.2f}R" for x in p.reward_risk_ratios)
    lines += [
        f"- Vade: {p.horizon.value} · Giriş bölgesi: **{p.entry_low} – {p.entry_high}**",
        f"- Stop-loss: **{p.stop_loss}** (yapısal seviye + ATR tamponu)",
        f"- Hedefler: {', '.join(str(t) for t in p.take_profits)} ({rrs})",
        f"- R:R (son hedef): **{float(p.risk_reward):.2f}**",
        f"- Geçersizlik: {p.invalidation}",
        "- Gerekçeler:",
        *[f"  - {x}" for x in p.reasons],
    ]
    return lines


def render_report(
    mtf: MultiTimeframeAnalysis,
    results: list[PlanResult],
    best: PlanResult | None,
    sources: dict[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    lines = [
        f"# {mtf.symbol} — Teknik Analiz Raporu",
        f"_{mtf.exchange} · {now:%Y-%m-%d %H:%M} UTC_",
        "",
        "> Bu rapor otomatik üretilmiştir; yatırım tavsiyesi değildir. Kazanç garanti edilmez.",
        "",
        "## Özet",
        f"- Üst zaman dilimi eğilimi: **{mtf.htf_bias.label_tr}**",
    ]
    for g in ("long", "mid", "short"):
        tfs = [tf for tf in mtf.groups.get(g, []) if tf in mtf.frames]
        if tfs:
            lines.append(f"- {GROUP_TR[g]} ({', '.join(tfs)}): {mtf.group_bias(g).label_tr}")
    if best is not None and best.plan is not None:
        p = best.plan
        lines.append(
            f"- **Önerilen plan: {p.side.value.upper()}** {p.timeframe} — puan "
            f"{float(p.confluence_score):.1f}, R:R {float(p.risk_reward):.2f}"
        )
    else:
        lines.append("- **Şu an eşikleri geçen bir işlem planı yok.**")
    if mtf.missing:
        lines.append(f"- Veri yok: {', '.join(mtf.missing)}")
    if sources:
        lines.append("- Veri kaynağı: " + ", ".join(f"{k}={v}" for k, v in sources.items()))
    lines += ["", "## Zaman dilimleri"]
    for g in ("long", "mid", "short"):
        tfs = [tf for tf in mtf.groups.get(g, []) if tf in mtf.frames]
        if not tfs:
            continue
        lines.append(f"### {GROUP_TR[g]}")
        for tf in tfs:
            lines += timeframe_section(mtf.frames[tf])
        lines.append("")
    lines.append("## İşlem planı adayları")
    for r in results:
        lines += plan_section(r)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
