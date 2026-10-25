from __future__ import annotations

from datetime import datetime
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from bot.core.models import (
    Candle,
    Fill,
    Horizon,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    Side,
    Signal,
    Ticker,
    TradePlan,
)


def candle(now: datetime, **kw: object) -> Candle:
    base: dict[str, object] = {
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "open_time": now,
        "open": "100",
        "high": "110",
        "low": "95",
        "close": "105",
        "volume": "12.5",
    }
    base.update(kw)
    return Candle.model_validate(base)


def test_candle_valid_and_decimal(now: datetime) -> None:
    c = candle(now)
    assert isinstance(c.close, D)
    assert c.close == D("105")


@pytest.mark.parametrize("kw", [{"low": "101"}, {"high": "104"}, {"volume": "-1"}])
def test_candle_invalid(now: datetime, kw: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        candle(now, **kw)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        candle(datetime(2026, 1, 1))


def test_models_are_frozen(now: datetime) -> None:
    c = candle(now)
    with pytest.raises(ValidationError):
        c.close = D("1")  # type: ignore[misc]


def test_float_input_kept_exact_via_str(now: datetime) -> None:
    t = Ticker(exchange="b", symbol="X/Y", timestamp=now, bid=D("0.1"), ask=D("0.3"), last=D("0.2"))
    assert t.mid == D("0.2")
    assert t.spread == D("0.2")
    assert t.spread_pct == D("100")


def test_ticker_crossed_rejected(now: datetime) -> None:
    with pytest.raises(ValidationError):
        Ticker(exchange="b", symbol="X/Y", timestamp=now, bid=D(2), ask=D(1), last=D(1))


def lvl(p: str, a: str = "1") -> OrderBookLevel:
    return OrderBookLevel(price=D(p), amount=D(a))


def test_orderbook_best_levels(now: datetime) -> None:
    ob = OrderBook(
        exchange="b",
        symbol="X/Y",
        timestamp=now,
        bids=(lvl("99"), lvl("98")),
        asks=(lvl("100"), lvl("101")),
    )
    assert ob.best_bid == lvl("99")
    assert ob.best_ask == lvl("100")
    empty = OrderBook(exchange="b", symbol="X/Y", timestamp=now)
    assert empty.best_bid is None and empty.best_ask is None


@pytest.mark.parametrize(
    ("bids", "asks"),
    [
        ((lvl("98"), lvl("99")), (lvl("100"),)),
        ((lvl("99"),), (lvl("101"), lvl("100"))),
        ((lvl("101"),), (lvl("100"),)),
    ],
)
def test_orderbook_invalid(
    now: datetime, bids: tuple[OrderBookLevel, ...], asks: tuple[OrderBookLevel, ...]
) -> None:
    with pytest.raises(ValidationError):
        OrderBook(exchange="b", symbol="X/Y", timestamp=now, bids=bids, asks=asks)


def intent(**kw: object) -> OrderIntent:
    base: dict[str, object] = {
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "side": "buy",
        "order_type": "limit",
        "amount": "0.01",
        "price": "100",
    }
    base.update(kw)
    return OrderIntent.model_validate(base)


def test_intent_valid_with_sl_tp() -> None:
    i = intent(stop_loss="95", take_profit="110")
    assert i.intent_id
    assert intent().intent_id != i.intent_id


@pytest.mark.parametrize(
    "kw",
    [
        {"price": None},
        {"order_type": "stop_market", "price": None},
        {"amount": "0"},
        {"stop_loss": "105"},
        {"take_profit": "90"},
        {"side": "sell", "stop_loss": "95"},
        {"side": "sell", "take_profit": "105"},
    ],
)
def test_intent_invalid(kw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        intent(**kw)


def test_market_intent_without_price() -> None:
    i = intent(order_type="market", price=None, stop_loss="95")
    assert i.order_type is OrderType.MARKET


def test_order_remaining_and_overfill(now: datetime) -> None:
    o = Order(
        client_order_id="c1",
        exchange="b",
        symbol="X/Y",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        amount=D("2"),
        price=D("10"),
        filled=D("0.5"),
        status=OrderStatus.PARTIALLY_FILLED,
        created_at=now,
    )
    assert o.remaining == D("1.5")
    assert not o.status.is_terminal
    assert OrderStatus.FILLED.is_terminal
    with pytest.raises(ValidationError):
        Order(
            client_order_id="c1",
            exchange="b",
            symbol="X/Y",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            amount=D("1"),
            filled=D("2"),
            created_at=now,
        )


def test_fill_notional(now: datetime) -> None:
    f = Fill(
        trade_id="t",
        client_order_id="c",
        exchange="b",
        symbol="X/Y",
        side=Side.SELL,
        price=D("2.5"),
        amount=D("4"),
        fee=D("0.01"),
        timestamp=now,
    )
    assert f.notional == D("10.0")


def test_position_pnl_and_risk(now: datetime) -> None:
    long = Position(
        exchange="b",
        symbol="X/Y",
        side=PositionSide.LONG,
        amount=D("2"),
        entry_price=D("100"),
        stop_loss=D("95"),
        opened_at=now,
    )
    assert long.unrealized_pnl(D("110")) == D("20")
    assert long.risk_at_stop == D("10")
    short = Position(
        exchange="b",
        symbol="X/Y",
        side=PositionSide.SHORT,
        amount=D("2"),
        entry_price=D("100"),
        stop_loss=D("105"),
        opened_at=now,
    )
    assert short.unrealized_pnl(D("90")) == D("20")
    assert PositionSide.SHORT.entry_side is Side.SELL
    assert PositionSide.LONG.exit_side is Side.SELL


@pytest.mark.parametrize(("side", "sl"), [(PositionSide.LONG, "100"), (PositionSide.SHORT, "99")])
def test_position_stop_wrong_side(now: datetime, side: PositionSide, sl: str) -> None:
    with pytest.raises(ValidationError):
        Position(
            exchange="b",
            symbol="X/Y",
            side=side,
            amount=D("1"),
            entry_price=D("100"),
            stop_loss=D(sl),
            opened_at=now,
        )


def plan(now: datetime, **kw: object) -> TradePlan:
    base: dict[str, object] = {
        "symbol": "BTC/USDT",
        "exchange": "binance",
        "side": "long",
        "horizon": "swing",
        "timeframe": "4h",
        "entry_low": "99",
        "entry_high": "101",
        "stop_loss": "95",
        "take_profits": ["105", "110", "115"],
        "confluence_score": "75",
        "reasons": ["HTF trend yukarı"],
        "invalidation": "4h kapanış < 95",
        "created_at": now,
    }
    base.update(kw)
    return TradePlan.model_validate(base)


def test_trade_plan_risk_reward(now: datetime) -> None:
    p = plan(now)
    assert p.entry_mid == D("100")
    assert p.risk_per_unit == D("5")
    assert p.reward_risk_ratios == (D("1"), D("2"), D("3"))
    assert p.risk_reward == D("3")
    assert p.horizon is Horizon.SWING


def test_short_trade_plan(now: datetime) -> None:
    p = plan(now, side="short", stop_loss="105", take_profits=["95", "90"])
    assert p.risk_reward == D("2")


@pytest.mark.parametrize(
    "kw",
    [
        {"entry_low": "102"},
        {"stop_loss": "99.5"},
        {"take_profits": ["110", "105"]},
        {"take_profits": ["100"]},
        {"take_profits": []},
        {"take_profits": ["105", "110", "115", "120"]},
        {"confluence_score": "101"},
        {"side": "short"},
    ],
)
def test_trade_plan_invalid(now: datetime, kw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        plan(now, **kw)


def test_signal_with_plan(now: datetime) -> None:
    s = Signal(
        strategy="ema_crossover",
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="1h",
        side=PositionSide.LONG,
        score=D("80"),
        timestamp=now,
        plan=plan(now),
    )
    assert s.plan is not None and s.plan.risk_reward == D("3")
