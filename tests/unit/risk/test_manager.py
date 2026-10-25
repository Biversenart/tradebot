from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bot.config.schema import AppConfig
from bot.core.clock import ManualClock
from bot.core.models import (
    Horizon,
    MarketInfo,
    MarketPrecision,
    OrderBook,
    OrderBookLevel,
    OrderIntent,
    OrderType,
    PositionSide,
    Side,
    Signal,
    Ticker,
    TradePlan,
)
from bot.risk.kill_switch import KillReason, KillSwitch
from bot.risk.manager import ApprovedIntent, MarketContext, RiskManager
from bot.risk.portfolio import PortfolioView, PositionView, historical_var, portfolio_risk
from bot.risk.report import Decision, RiskScore
from bot.risk.sizing import GrowthSizer

T = datetime(2026, 1, 1, tzinfo=UTC)
INFO = MarketInfo(
    exchange="binance",
    symbol="BTC/USDT",
    base="BTC",
    quote="USDT",
    precision=MarketPrecision(tick_size=D("0.01"), lot_size=D("0.001"), min_notional=D(10)),
)


def sig(
    symbol: str = "BTC/USDT",
    side: PositionSide = PositionSide.LONG,
    score: str = "80",
    stop: str = "95",
) -> Signal:
    long = side is PositionSide.LONG
    plan = TradePlan(
        symbol=symbol,
        exchange="binance",
        side=side,
        horizon=Horizon.SWING,
        timeframe="1h",
        entry_low=D(99) if long else D(100),
        entry_high=D(100) if long else D(101),
        stop_loss=D(stop),
        take_profits=(D(110),) if long else (D(90),),
        confluence_score=D(score),
        invalidation="x",
        created_at=T,
    )
    return Signal(
        strategy="s",
        exchange="binance",
        symbol=symbol,
        timeframe="1h",
        side=side,
        score=D(score),
        timestamp=T,
        plan=plan,
    )


def manager(gate: bool = True, market_type: str = "spot", **risk: Any) -> RiskManager:
    cfg = AppConfig.model_validate(
        {"risk": {"growth": {"profit_lock": {"enabled": False}}, **risk}}
    )
    ks = KillSwitch(cfg.risk, ManualClock(T))
    return RiskManager(cfg, ks, GrowthSizer(cfg.risk, D(10_000)), lambda: gate, market_type)


def book(depth: str = "100") -> OrderBook:
    return OrderBook(
        exchange="binance",
        symbol="BTC/USDT",
        timestamp=T,
        bids=(OrderBookLevel(price=D("99.9"), amount=D(depth)),),
        asks=(OrderBookLevel(price=D(100), amount=D(depth)),),
    )


def ctx(**kw: Any) -> MarketContext:
    return MarketContext(market=INFO, **kw)


EMPTY = PortfolioView(equity=D(10_000))


def test_approves_and_sizes() -> None:
    d = manager().assess_signal(sig(), EMPTY, ctx(book=book()))
    assert d.ok and isinstance(d.approved, ApprovedIntent)
    i = d.approved.intent
    assert i.amount == D(20)  # 1% of 10k / (100 - 95)
    assert i.stop_loss == D(95) and i.side is Side.BUY
    r = d.report
    assert r.loss_at_stop == D(100) and r.loss_at_stop_pct == D(1)
    assert r.decision is Decision.APPROVE and r.score is RiskScore.LOW
    assert r.expected_costs > 0


def test_approved_intent_cannot_be_forged() -> None:
    intent = OrderIntent(
        exchange="binance",
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        amount=D(1),
    )
    with pytest.raises(PermissionError):
        ApprovedIntent(intent, None, T)


def test_gate_and_kill_switch_block() -> None:
    assert "egress" in manager(gate=False).assess_signal(sig(), EMPTY, ctx()).report.reasons[0]
    m = manager()
    import asyncio

    asyncio.run(m.kill_switch.trigger(KillReason.MANUAL, "test"))
    d = m.assess_signal(sig(), EMPTY, ctx())
    assert not d.ok and "kill switch" in d.report.reasons[0]


def test_rejects_short_on_spot_and_wrong_stop() -> None:
    assert not manager().assess_signal(sig(side=PositionSide.SHORT, stop="105"), EMPTY, ctx()).ok
    assert (
        manager(market_type="futures")
        .assess_signal(sig(side=PositionSide.SHORT, stop="105"), EMPTY, ctx())
        .ok
    )
    t = Ticker(
        exchange="binance", symbol="BTC/USDT", timestamp=T, bid=D("94.9"), ask=D(95), last=D(95)
    )
    d = manager().assess_signal(sig(), EMPTY, ctx(ticker=t))
    assert not d.ok and "yanlış tarafında" in d.report.reasons[0]


def test_spread_filter() -> None:
    t = Ticker(
        exchange="binance", symbol="BTC/USDT", timestamp=T, bid=D(99), ask=D(100), last=D("99.5")
    )
    d = manager().assess_signal(sig(), EMPTY, ctx(ticker=t))
    assert not d.ok and "spread" in d.report.reasons[0]


def pos(symbol: str, qty: str = "10", mark: str = "100") -> PositionView:
    return PositionView(symbol, PositionSide.LONG, D(qty), D(100), D(95), D(mark))


def test_max_open_positions() -> None:
    pv = PortfolioView(D(10_000), [pos(f"C{i}/USDT", "1") for i in range(5)])
    assert "maksimum açık pozisyon" in manager().assess_signal(sig(), pv, ctx()).report.reasons[0]


def test_exposure_caps_resize() -> None:
    pv = PortfolioView(D(10_000), [pos("BTC/USDT", "15")])  # 1500 of 2000 symbol cap used
    d = manager().assess_signal(sig(), pv, ctx())
    assert d.ok and d.approved is not None
    assert d.approved.intent.amount == D(5)  # 500 room / 100
    assert d.report.decision is Decision.RESIZE
    full = PortfolioView(D(10_000), [pos("BTC/USDT", "20")])
    assert not manager().assess_signal(sig(), full, ctx()).ok


def test_liquidity_resize_and_min_notional() -> None:
    d = manager().assess_signal(sig(), EMPTY, ctx(book=book("30")))  # 3000 depth / ratio 3
    assert d.ok and d.approved is not None and d.approved.intent.amount == D(10)
    assert d.report.liquidity_score is not None and d.report.liquidity_score < 1
    d2 = manager().assess_signal(sig(), EMPTY, ctx(book=book("0.25")))
    assert not d2.ok


def correlated_closes(rho_high: bool) -> dict[str, pd.Series]:
    rng = np.random.default_rng(1)
    base = rng.normal(0, 0.01, 800)
    other = base + rng.normal(0, 0.001 if rho_high else 0.05, 800)
    idx = pd.date_range("2025", periods=800, freq="1h", tz="UTC")
    return {
        "BTC/USDT": pd.Series(100 * np.exp(np.cumsum(base)), index=idx),
        "ETH/USDT": pd.Series(100 * np.exp(np.cumsum(other)), index=idx),
        "SOL/USDT": pd.Series(100 * np.exp(np.cumsum(other * 1.1)), index=idx),
    }


def test_correlation_reduces_then_rejects() -> None:
    closes = correlated_closes(True)
    pv1 = PortfolioView(D(10_000), [pos("ETH/USDT", "1")])
    d = manager().assess_signal(sig(), pv1, ctx(closes=closes))
    assert d.ok and d.approved is not None and d.approved.intent.amount == D(10)  # halved
    assert d.report.correlated_with == ["ETH/USDT"]
    pv2 = PortfolioView(D(10_000), [pos("ETH/USDT", "1"), pos("SOL/USDT", "1")])
    assert not manager().assess_signal(sig(), pv2, ctx(closes=closes)).ok
    low = correlated_closes(False)
    d3 = manager().assess_signal(sig(), pv1, ctx(closes=low))
    assert d3.ok and d3.approved is not None and d3.approved.intent.amount == D(20)


def test_exit_always_allowed_but_bounded() -> None:
    m = manager(gate=False)
    exit_intent = OrderIntent(
        exchange="binance",
        symbol="BTC/USDT",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        amount=D(5),
        reduce_only=True,
    )
    assert m.assess_exit(exit_intent, D(10)).ok
    assert not m.assess_exit(exit_intent, D(1)).ok
    assert not m.assess_exit(exit_intent.model_copy(update={"reduce_only": False}), D(10)).ok


def test_assess_intent_caps() -> None:
    m = manager()
    i = OrderIntent(
        exchange="binance",
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        amount=D(10),
        price=D(100),
        strategy="arb",
    )
    assert m.assess_intent(i, EMPTY, ctx()).ok
    assert not m.assess_intent(i.model_copy(update={"amount": D(25)}), EMPTY, ctx()).ok


def test_portfolio_risk() -> None:
    closes = correlated_closes(True)
    pv = PortfolioView(
        D(10_000),
        [pos("BTC/USDT", "10"), pos("ETH/USDT", "10", "110")],
        balances={"USDT": D(7_000), "BTC": D(1_000), "ETH": D(2_000)},
    )
    pr = portfolio_risk(pv, closes, lookback=500)
    assert pr.total_exposure == D(2_100) and pr.exposure_pct == D(21)
    assert pr.risk_at_stop == D(50) + D(150)
    assert pr.stablecoin_pct == D(70)
    assert pr.var is not None and pr.cvar is not None and pr.cvar >= pr.var > 0
    assert pr.correlation is not None
    assert float(pr.correlation.to_numpy()[0, 1]) > 0.9
    assert historical_var([], closes, 100, 0.95) == (0.0, 0.0)
