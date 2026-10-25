from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from bot.analysis.chart import build_figure, write_chart
from bot.analysis.confluence import score_setup
from bot.analysis.engine import MultiTimeframeAnalysis, analyze_symbol, analyze_timeframe
from bot.analysis.plan import best_plan, build_plan, to_price
from bot.analysis.report import render_report
from bot.analysis.structure import Direction
from bot.config.schema import AnalysisConfig
from bot.core.models import Horizon, PositionSide
from tests.fixtures.loader import synthetic_1h

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DF = synthetic_1h()
UP = DF.iloc[:390]
DOWN = DF.iloc[700:1180]


def cfg(**over: Any) -> AnalysisConfig:
    base: dict[str, Any] = {"timeframes": {"long": ["1d"], "mid": ["1h"], "short": []}}
    for k, v in over.items():
        base[k] = v
    return AnalysisConfig.model_validate(base)


def mtf(
    htf: pd.DataFrame, setup: pd.DataFrame, c: AnalysisConfig | None = None
) -> MultiTimeframeAnalysis:
    return analyze_symbol({"1d": htf, "1h": setup}, "BTC/USDT", "binance", c or cfg())


def test_timeframe_bias_follows_fixture_segments() -> None:
    assert analyze_timeframe(UP, "1h").bias is Direction.BULLISH
    assert analyze_timeframe(DOWN, "1h").bias is Direction.BEARISH


def test_timeframe_analysis_contents() -> None:
    ta = analyze_timeframe(DF.iloc[:800], "1h")
    assert ta.ind.atr > 0 and ta.ind.rsi is not None
    assert set(ta.ind.emas) == {9, 21, 50, 200}
    assert ta.structure.swings and ta.zones
    assert ta.profile is not None and ta.profile.val <= ta.profile.poc <= ta.profile.vah
    assert len(ta.df) == AnalysisConfig().analysis_bars


def test_too_few_bars() -> None:
    with pytest.raises(ValueError):
        analyze_timeframe(DF.iloc[:10], "1h")


def test_mtf_groups_and_missing() -> None:
    m = analyze_symbol({"1h": UP}, "BTC/USDT")
    assert "1h" in m.frames
    assert set(m.missing) == {"1w", "1d", "4h", "15m", "5m"}
    assert m.setup_timeframe() == "1h"
    assert m.horizon_of("1h") is Horizon.SWING
    assert m.horizon_of("5m") is Horizon.SCALP
    assert m.htf_bias is Direction.NEUTRAL  # no long-term data


def test_confluence_components_and_bounds() -> None:
    m = mtf(UP, UP)
    s = score_setup(m, "1h", Direction.BULLISH)
    assert {c.name for c in s.components} == {
        "htf_trend_alignment",
        "zone_proximity",
        "structure_confirmation",
        "volume_confirmation",
        "pattern",
        "divergence",
        "regime_fit",
    }
    assert 0 <= s.total <= 100
    assert s.penalty == 1.0
    htf = next(c for c in s.components if c.name == "htf_trend_alignment")
    assert htf.score == 1.0
    with pytest.raises(ValueError):
        score_setup(m, "1h", Direction.NEUTRAL)


def test_htf_conflict_penalty() -> None:
    m = mtf(DOWN, UP)
    assert m.htf_bias is Direction.BEARISH
    s = score_setup(m, "1h", Direction.BULLISH)
    assert s.penalty == 0.5
    assert s.total == pytest.approx(s.raw * 0.5, abs=0.01)
    assert any("UYARI" in r for r in s.reasons)


def test_custom_weights() -> None:
    c = cfg(
        confluence={
            "weights": {
                k: 0
                for k in (
                    "zone_proximity",
                    "structure_confirmation",
                    "volume_confirmation",
                    "pattern",
                    "divergence",
                    "regime_fit",
                )
            }
            | {"htf_trend_alignment": 1}
        }
    )
    s = score_setup(mtf(UP, UP, c), "1h", Direction.BULLISH, c)
    assert s.total == 100.0
    with pytest.raises(ValidationError):
        cfg(confluence={"weights": {"made_up": 5}})


def lenient(**tp: Any) -> AnalysisConfig:
    return cfg(
        confluence={"min_score": 0}, trade_plan={"min_risk_reward": 1.0, "max_stop_atr": 50, **tp}
    )


def test_accepted_long_plan_geometry() -> None:
    c = lenient()
    r = build_plan(mtf(UP, UP, c), "1h", Direction.BULLISH, c, NOW)
    assert r.accepted, r.rejected
    p = r.plan
    assert p is not None
    assert p.side is PositionSide.LONG and p.horizon is Horizon.SWING
    assert p.stop_loss < p.entry_low <= p.entry_high < p.take_profits[0]
    assert [round(float(x), 3) for x in p.reward_risk_ratios] == [1.0, 2.0, 3.0]
    assert p.risk_reward >= Decimal(1)
    assert "kapanışı" in p.invalidation and p.created_at == NOW
    assert p.reasons


def test_short_plan_mirrors() -> None:
    c = lenient()
    r = build_plan(mtf(DOWN, DOWN, c), "1h", Direction.BEARISH, c, NOW)
    assert r.stop is not None and r.entry_high is not None
    assert r.stop > r.entry_high
    assert all(t < (r.entry_low or 0) for t in r.targets)


def test_rejections() -> None:
    m = mtf(UP, UP)
    low_rr = cfg(trade_plan={"take_profits": [1], "min_risk_reward": 2, "max_stop_atr": 50})
    assert "R:R" in (build_plan(m, "1h", Direction.BULLISH, low_rr).rejected or "")
    tight = cfg(trade_plan={"max_stop_atr": 0.01})
    assert "stop çok uzak" in (build_plan(m, "1h", Direction.BULLISH, tight).rejected or "")
    strict = cfg(
        confluence={"min_score": 100}, trade_plan={"min_risk_reward": 1, "max_stop_atr": 50}
    )
    r = build_plan(m, "1h", Direction.BULLISH, strict)
    assert r.plan is None and r.rejected


def test_best_plan_picks_accepted_highest() -> None:
    c = lenient()
    best, results = best_plan(mtf(UP, UP, c), c, NOW)
    assert len(results) == 2
    accepted = [r for r in results if r.accepted]
    if accepted:
        assert best is not None and best.score.total == max(r.score.total for r in accepted)


def test_to_price() -> None:
    assert to_price(123.456789123) == Decimal("123.45679")
    assert to_price(0.000012345678) == Decimal("0.000012345678")


def test_report_and_chart(tmp_path: Path) -> None:
    c = lenient()
    m = mtf(UP, UP, c)
    best, results = best_plan(m, c, NOW)
    text = render_report(m, results, best, {"1h": "parquet"}, NOW)
    assert text.startswith("# BTC/USDT — Teknik Analiz Raporu")
    for heading in (
        "## Özet",
        "## Zaman dilimleri",
        "## İşlem planı adayları",
        "yatırım tavsiyesi",
    ):
        assert heading in text
    assert "Stop-loss" in text or "Plan yok" in text
    fig = build_figure(m.frames["1h"], best or results[0], "BTC/USDT 1h", bars=100)
    kinds = {t.type for t in fig.data}
    assert "candlestick" in kinds
    assert len(fig.layout.shapes) > 0
    path = write_chart(fig, tmp_path / "x" / "chart.html")
    html = path.read_text("utf-8")
    assert "plotly" in html.lower() and "STOP" in html
