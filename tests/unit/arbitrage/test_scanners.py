from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from bot.arbitrage.cross import Venue, evaluate_pair, scan_cross
from bot.arbitrage.rebalance import rebalance_advice
from bot.arbitrage.triangular import Market, find_cycles, scan_triangular, simulate
from bot.config.schema import CrossExchangeConfig, TriangularConfig
from bot.core.models import OrderBook, OrderBookLevel, Side

NOW = datetime(2026, 1, 1, tzinfo=UTC)
FEE = D("0.001")


def ob(
    ex: str, sym: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]], age_ms: int = 0
) -> OrderBook:
    return OrderBook(
        exchange=ex,
        symbol=sym,
        timestamp=NOW - timedelta(milliseconds=age_ms),
        bids=tuple(OrderBookLevel(price=D(p), amount=D(a)) for p, a in bids),
        asks=tuple(OrderBookLevel(price=D(p), amount=D(a)) for p, a in asks),
    )


CFG = CrossExchangeConfig(
    enabled=True,
    pairs=("BTC/USDT",),
    min_net_spread_pct=D("0.25"),
    slippage_buffer_pct=D("0.05"),
    max_notional_quote=D(100_000),
)


def venue(ex: str, book: OrderBook, **kw: object) -> Venue:
    return Venue(ex, "BTC/USDT", book, FEE, **kw)  # type: ignore[arg-type]


def test_net_spread_formula_known_values() -> None:
    a = venue("A", ob("A", "BTC/USDT", [("99", "5")], [("100", "1")]))
    b = venue("B", ob("B", "BTC/USDT", [("101", "1")], [("102", "5")]))
    opp = evaluate_pair(a, b, CFG, NOW)
    assert opp is not None
    # gross 1% - 0.1% - 0.1% - 0.05% = 0.75%
    assert opp.gross_pct == D(1) and opp.net_pct == D("0.75")
    assert [lg.side for lg in opp.legs] == [Side.BUY, Side.SELL]
    assert opp.notional_quote == D(100) and opp.expected_profit_quote == D("0.75")


def test_fees_kill_small_spread() -> None:
    a = venue("A", ob("A", "BTC/USDT", [("99", "5")], [("100", "1")]))
    b = venue("B", ob("B", "BTC/USDT", [("100.4", "1")], [("102", "5")]))
    assert evaluate_pair(a, b, CFG, NOW) is None  # 0.4% gross - 0.25% costs < 0.25% min


def test_depth_limits_size_by_marginal_level() -> None:
    a = venue("A", ob("A", "BTC/USDT", [("99", "5")], [("100", "1"), ("100.5", "2"), ("101", "5")]))
    b = venue(
        "B", ob("B", "BTC/USDT", [("101.5", "1"), ("101.2", "2"), ("100", "5")], [("103", "5")])
    )
    opp = evaluate_pair(a, b, CFG, NOW)
    assert opp is not None
    # level 2 (buy 100.5 / sell 101.2 -> 0.70% gross, 0.45% net) still clears; level 3 does not
    assert opp.legs[0].amount == D(3)
    assert opp.depth_limited
    assert opp.legs[0].worst_price == D("100.5") and opp.legs[1].worst_price == D("101.2")


def test_inventory_caps_size() -> None:
    a = venue("A", ob("A", "BTC/USDT", [("99", "5")], [("100", "10")]), quote_balance=D(250))
    b = venue("B", ob("B", "BTC/USDT", [("101", "10")], [("102", "5")]), base_balance=D(5))
    opp = evaluate_pair(a, b, CFG, NOW)
    assert opp is not None and opp.legs[0].amount == D("2.5")


def test_stale_book_rejected() -> None:
    a = venue("A", ob("A", "BTC/USDT", [("99", "5")], [("100", "1")], age_ms=600))
    b = venue("B", ob("B", "BTC/USDT", [("101", "1")], [("102", "5")]))
    assert evaluate_pair(a, b, CFG, NOW) is None


def test_scan_cross_both_directions_and_try_bridge() -> None:
    usdt = venue("binance", ob("binance", "BTC/USDT", [("99", "5")], [("100", "1")]))
    # BTC/TRY 3333 at 32 TRY/USDT = 104.2 USDT -> buy on binance, sell on btcturk
    tr = Venue(
        "btcturk",
        "BTC/TRY",
        ob("btcturk", "BTC/TRY", [("3333", "1")], [("3400", "1")]),
        FEE,
        fx=D(1) / D(32),
    )
    opps = scan_cross({"binance": usdt, "btcturk": tr}, CFG, NOW)
    assert len(opps) == 1
    o = opps[0]
    assert o.route == "BTC/USDT@binance -> BTC/TRY@btcturk"
    assert o.legs[1].avg_price == D(3333)  # reported in the venue's own currency
    assert o.net_pct > D(3)


def tri_markets(eth_btc_bid: str) -> dict[str, Market]:
    return {
        "BTC/USDT": Market(
            "BTC/USDT", "BTC", "USDT", ob("x", "BTC/USDT", [("99.9", "100")], [("100", "100")]), FEE
        ),
        "ETH/BTC": Market(
            "ETH/BTC",
            "ETH",
            "BTC",
            ob("x", "ETH/BTC", [(eth_btc_bid, "1000")], [("0.0501", "1000")]),
            FEE,
        ),
        "ETH/USDT": Market(
            "ETH/USDT",
            "ETH",
            "USDT",
            ob("x", "ETH/USDT", [("5.1", "1000")], [("5.11", "1000")]),
            FEE,
        ),
    }


def test_find_cycles() -> None:
    cycles = find_cycles(tri_markets("0.05"), "USDT")
    routes = {tuple(m.symbol for m in c) for c in cycles}
    assert ("BTC/USDT", "ETH/BTC", "ETH/USDT") in routes
    assert ("ETH/USDT", "ETH/BTC", "BTC/USDT") in routes


def test_triangular_known_profit() -> None:
    # USDT->BTC @100 (ask), BTC->ETH buy @0.0501, ETH->USDT sell @5.1
    cycle = tuple(tri_markets("0.05")[s] for s in ("BTC/USDT", "ETH/BTC", "ETH/USDT"))
    res = simulate(cycle, "USDT", D(100))  # type: ignore[arg-type]
    assert res is not None
    end, _legs = res
    expected = D(1) * (1 - FEE) / D("0.0501") * (1 - FEE) * D("5.1") * (1 - FEE)
    assert end == pytest.approx(expected)
    cfg = TriangularConfig(
        enabled=True,
        min_net_profit_pct=D("0.5"),
        max_notional_quote=D(100),
        symbols=("BTC/USDT", "ETH/BTC", "ETH/USDT"),
    )
    opps = scan_triangular(tri_markets("0.05"), cfg, NOW, "x")
    assert opps and opps[0].route == "BTC/USDT -> ETH/BTC -> ETH/USDT@x"
    assert opps[0].net_pct == pytest.approx(expected - 100)  # % of 100
    assert len(opps[0].legs) == 3


def test_triangular_none_when_fair() -> None:
    m = tri_markets("0.05")
    m["ETH/USDT"] = Market(
        "ETH/USDT", "ETH", "USDT", ob("x", "ETH/USDT", [("5.0", "1000")], [("5.01", "1000")]), FEE
    )
    cfg = TriangularConfig(enabled=True, min_net_profit_pct=D("0.1"))
    assert scan_triangular(m, cfg, NOW, "x") == []


def test_triangular_skips_stale() -> None:
    m = tri_markets("0.05")
    old = m["ETH/USDT"]
    m["ETH/USDT"] = Market(
        old.symbol,
        old.base,
        old.quote,
        ob("x", "ETH/USDT", [("5.1", "1000")], [("5.11", "1000")], age_ms=5000),
        FEE,
    )
    assert scan_triangular(m, TriangularConfig(enabled=True), NOW, "x") == []


def test_rebalance_advice() -> None:
    inv = {"A": {"BTC": D(90), "USDT": D(10)}, "B": {"BTC": D(10), "USDT": D(90)}}
    adv = rebalance_advice(inv, "BTC", "USDT", D(20))
    assert {(a.exchange, a.asset_low) for a in adv} == {("A", "USDT"), ("B", "BTC")}
    assert "manuel" in adv[0].message and "para çekmez" in adv[0].message
    assert rebalance_advice({"A": {"BTC": D(50), "USDT": D(50)}}, "BTC", "USDT", D(20)) == []
