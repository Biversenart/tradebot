from __future__ import annotations

from bot.backtest.sizing_compare import compare_sizing, sizing_markdown
from bot.config.schema import AppConfig
from tests.fixtures.loader import synthetic_1h


def test_compare_sizing_same_signals() -> None:
    cfg = AppConfig()
    rows = compare_sizing({"BTC/USDT": synthetic_1h()}, ["breakout"], cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r.fixed.trades > 0 and r.growth.trades > 0
    md = sizing_markdown(rows, "fixture")
    assert "| BTC/USDT | breakout |" in md and "Büyüme boyutlaması" in md
