from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any

import pytest

from bot.config.schema import RiskConfig
from bot.core.models import Horizon, PositionSide, Signal, TradePlan
from bot.risk.sizing import GrowthSizer, ProfitLock

T = datetime(2026, 1, 1, tzinfo=UTC)


def signal(
    score: str = "80", strategy: str = "s", symbol: str = "BTC/USDT", when: datetime = T
) -> Signal:
    plan = TradePlan(
        symbol=symbol,
        exchange="binance",
        side=PositionSide.LONG,
        horizon=Horizon.SWING,
        timeframe="1h",
        entry_low=D(99),
        entry_high=D(100),
        stop_loss=D(95),
        take_profits=(D(110),),
        confluence_score=D(score),
        invalidation="x",
        created_at=when,
    )
    return Signal(
        strategy=strategy,
        exchange="binance",
        symbol=symbol,
        timeframe="1h",
        side=PositionSide.LONG,
        score=D(score),
        timestamp=when,
        plan=plan,
    )


def risk(**g: Any) -> RiskConfig:
    return RiskConfig.model_validate({"growth": g} if g else {})


def test_base_risk_and_compounding() -> None:
    s = GrowthSizer(risk(profit_lock={"enabled": False}), D(10_000))
    qty = s.size(D(10_000), D(100), D(95), signal("80"))
    assert qty == D(10_000) * D(1) / 100 / D(5)  # 1% of equity / stop distance = 20 units
    qty2 = s.size(D(20_000), D(100), D(95), signal("80"))
    assert qty2 == 2 * qty  # compounding: size grows with equity


def test_no_compounding_uses_initial_equity() -> None:
    s = GrowthSizer(risk(compounding=False, profit_lock={"enabled": False}), D(10_000))
    assert s.size(D(20_000), D(100), D(95), signal()) == D(20)


@pytest.mark.parametrize(("score", "factor"), [("90", D("1.5")), ("80", D(1)), ("70", D("0.5"))])
def test_quality_multiplier(score: str, factor: D) -> None:
    s = GrowthSizer(risk(profit_lock={"enabled": False}), D(10_000))
    b = s.risk_pct(signal(score), D(10_000))
    assert b.quality == factor and b.final_risk_pct == D(1) * factor


def test_hard_ceiling_never_exceeded() -> None:
    s = GrowthSizer(
        risk(
            profit_lock={"enabled": False},
            auto_allocation={"min_trades": 1, "max_multiplier": "1.5"},
        ),
        D(10_000),
    )
    for _ in range(5):
        s.on_trade_closed("s", "BTC/USDT", D(100), D(2))
    b = s.risk_pct(signal("95"), D(10_000))
    assert b.allocation > 1
    assert b.final_risk_pct == D("1.5")  # 1% x 1.5 ceiling


def test_drawdown_scaling_and_gradual_recovery() -> None:
    s = GrowthSizer(risk(profit_lock={"enabled": False}), D(10_000))
    s.on_equity(D(10_000))
    assert s.drawdown_factor(D(9_400)) == D("0.5")  # dd 6% >= 5%
    mid = s.drawdown_factor(D(9_750))  # dd 2.5% -> halfway back
    assert mid == D("0.75")
    assert s.drawdown_factor(D(10_000)) == D(1)


def test_loss_streak() -> None:
    s = GrowthSizer(risk(profit_lock={"enabled": False}), D(10_000))
    for _ in range(3):
        s.on_trade_closed("s", "X", D(-10), D(-1))
    assert s.loss_streak_factor() == D("0.5")
    s.on_trade_closed("s", "X", D(10), D(1))
    assert s.loss_streak_factor() == D(1)


def test_auto_allocation_pauses_negative_expectancy() -> None:
    s = GrowthSizer(
        risk(profit_lock={"enabled": False}, auto_allocation={"min_trades": 5}), D(10_000)
    )
    for _ in range(5):
        s.on_trade_closed("bad", "BTC/USDT", D(-10), D(-1))
    assert s.size(D(10_000), D(100), D(95), signal(strategy="bad")) == 0
    assert s.last is not None and s.last.paused_reason is not None
    assert "duraklatıldı" in s.last.paused_reason
    # other strategy / symbol unaffected (allocation is per strategy x coin)
    assert s.size(D(10_000), D(100), D(95), signal(strategy="good")) > 0


def test_quarter_kelly_cap() -> None:
    s = GrowthSizer(
        risk(
            profit_lock={"enabled": False},
            kelly={"enabled": True, "fraction": "0.25", "min_trades": 100},
            auto_allocation={"enabled": False},
            loss_streak={"count": 1000},
        ),
        D(10_000),
    )
    # 40% win rate, avg win 2R, avg loss 1R -> f* = 0.4 - 0.6/2 = 0.1 -> 1/4 Kelly = 2.5%
    for i in range(100):
        if i % 5 < 2:
            s.on_trade_closed("k", "X", D(20), D(2))
        else:
            s.on_trade_closed("k", "X", D(-10), D(-1))
    assert s.kelly_cap_pct("k") == pytest.approx(D("2.5"))
    b = s.risk_pct(signal("80", strategy="k"), D(10_000))
    assert b.final_risk_pct == D(1)  # base below the Kelly cap
    # negative edge -> paused
    s2 = GrowthSizer(
        risk(
            profit_lock={"enabled": False},
            kelly={"enabled": True, "min_trades": 100},
            auto_allocation={"enabled": False},
            loss_streak={"count": 1000},
        ),
        D(10_000),
    )
    for i in range(100):
        s2.on_trade_closed("k", "X", D(10) if i % 5 == 0 else D(-10), D(1) if i % 5 == 0 else D(-1))
    assert s2.size(D(10_000), D(100), D(95), signal(strategy="k")) == 0


def test_profit_lock_reserve() -> None:
    lock = ProfitLock(D(20), D(25), D(10_000))
    lock.update(D(11_000))
    assert lock.reserve == 0
    lock.update(D(12_100))  # +20% -> lock 25% of 2000
    assert lock.reserve == D(500)
    lock.update(D(14_400))  # next step 14400 -> lock 25% of 2400
    assert lock.reserve == D(1100)
    s = GrowthSizer(risk(), D(10_000))
    s.on_equity(D(12_000))
    b = s.risk_pct(signal(), D(12_000))
    assert b.tradable_equity == D(12_000) - D(500)


def test_event_window() -> None:
    r = RiskConfig.model_validate(
        {
            "event_windows": [
                {
                    "label": "FOMC",
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-01-01T06:00:00Z",
                    "risk_factor": "0.5",
                }
            ],
            "growth": {"profit_lock": {"enabled": False}},
        }
    )
    s = GrowthSizer(r, D(10_000))
    b = s.risk_pct(signal(), D(10_000))
    assert b.event == D("0.5") and "FOMC" in b.notes[0]


def test_notional_cap() -> None:
    s = GrowthSizer(risk(profit_lock={"enabled": False}), D(10_000), max_notional_pct=D(10))
    assert s.size(D(10_000), D(100), D("99.9"), signal()) == D(10)  # capped to 1000 notional


def test_drawdown_factor_never_above_one_on_new_highs() -> None:
    s = GrowthSizer(risk(profit_lock={"enabled": False}), D(10_000))
    assert s.drawdown_factor(D(50_000)) == D(1)
    assert s.peak == D(50_000)


def test_paused_combination_gets_fresh_trial_after_cooldown() -> None:
    from datetime import timedelta

    r = risk(profit_lock={"enabled": False}, auto_allocation={"min_trades": 5, "pause_hours": 24})
    s = GrowthSizer(r, D(10_000))
    for _ in range(5):
        s.on_trade_closed("bad", "BTC/USDT", D(-10), D(-1))
    assert s.size(D(10_000), D(100), D(95), signal(strategy="bad", when=T)) == 0
    later = T + timedelta(hours=12)
    assert s.size(D(10_000), D(100), D(95), signal(strategy="bad", when=later)) == 0
    after = T + timedelta(hours=25)
    assert s.size(D(10_000), D(100), D(95), signal(strategy="bad", when=after)) > 0
