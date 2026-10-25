"""Annotated plotly chart: zones, structure, entry/stop/targets (spec §5.3.j)."""

from __future__ import annotations

from pathlib import Path

import plotly.graph_objects as go

from bot.analysis.engine import TimeframeAnalysis
from bot.analysis.plan import PlanResult
from bot.analysis.structure import SwingKind
from bot.indicators import ema


def build_figure(
    ta: TimeframeAnalysis, plan: PlanResult | None, title: str, bars: int = 200
) -> go.Figure:
    df = ta.df.iloc[-bars:]
    offset = len(ta.df) - len(df)
    x = df.index
    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=x,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name=ta.timeframe,
        )
    )
    for n, color in ((21, "#f39c12"), (50, "#2980b9"), (200, "#8e44ad")):
        if n in ta.ind.emas:
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=ema(ta.df["close"], n).iloc[-bars:],
                    name=f"EMA{n}",
                    line={"width": 1, "color": color},
                )
            )
    x0, x1 = x[0], x[-1]
    for z in ta.zones:
        color = "rgba(46,204,113,0.18)" if z.kind.is_bullish else "rgba(231,76,60,0.18)"
        start = x[max(0, z.first_index - offset)] if z.first_index >= offset else x0
        fig.add_shape(
            type="rect",
            x0=start,
            x1=x1,
            y0=z.low,
            y1=z.high,
            fillcolor=color,
            line={"width": 0},
            layer="below",
        )
        fig.add_annotation(
            x=x1,
            y=z.high,
            text=f"{z.kind.label_tr} ({z.strength:.0f})",
            showarrow=False,
            xanchor="left",
            font={"size": 9},
        )
    sw = [s for s in ta.structure.swings if s.index >= offset]
    for kind, sym, color in (
        (SwingKind.HIGH, "triangle-down", "#c0392b"),
        (SwingKind.LOW, "triangle-up", "#27ae60"),
    ):
        pts = [s for s in sw if s.kind is kind]
        if pts:
            fig.add_trace(
                go.Scatter(
                    x=[s.time for s in pts],
                    y=[s.price for s in pts],
                    mode="markers+text",
                    marker={"symbol": sym, "size": 9, "color": color},
                    text=[s.label or "" for s in pts],
                    textposition="top center" if kind is SwingKind.HIGH else "bottom center",
                    name=f"swing {kind.value}",
                )
            )
    for ev in ta.structure.events:
        if ev.index < offset or ev.swing_index < offset:
            continue
        fig.add_shape(
            type="line",
            x0=x[ev.swing_index - offset],
            x1=x[ev.index - offset],
            y0=ev.level,
            y1=ev.level,
            line={"dash": "dot", "width": 1, "color": "#34495e"},
        )
        fig.add_annotation(
            x=x[ev.index - offset], y=ev.level, text=ev.kind, showarrow=False, font={"size": 9}
        )
    if ta.profile:
        for name, lvl in (
            ("POC", ta.profile.poc),
            ("VAH", ta.profile.vah),
            ("VAL", ta.profile.val),
        ):
            fig.add_hline(
                y=lvl,
                line={"dash": "dash", "width": 1, "color": "#7f8c8d"},
                annotation_text=name,
                annotation_position="left",
            )
    if plan is not None and plan.entry_low is not None and plan.stop is not None:
        accepted = plan.accepted
        fig.add_shape(
            type="rect",
            x0=x[-min(20, len(x))],
            x1=x1,
            y0=plan.entry_low,
            y1=plan.entry_high,
            fillcolor="rgba(52,152,219,0.25)",
            line={"width": 1},
        )
        fig.add_hline(
            y=plan.stop,
            line={"color": "#e74c3c", "width": 2},
            annotation_text="STOP",
            annotation_position="right",
        )
        for i, t in enumerate(plan.targets, 1):
            fig.add_hline(
                y=t,
                line={"color": "#2ecc71", "width": 1.5, "dash": "dash"},
                annotation_text=f"TP{i}",
                annotation_position="right",
            )
        if not accepted:
            title += " — taslak (eşik altı)"
    fig.update_layout(
        title=title,
        xaxis_rangeslider_visible=False,
        template="plotly_white",
        height=760,
        legend={"orientation": "h"},
    )
    return fig


def write_chart(fig: go.Figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(path), include_plotlyjs="cdn", full_html=True)
    return path
