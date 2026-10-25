"""Leg-risk scenarios on real PaperExchanges."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from bot.arbitrage.cross import Venue, evaluate_pair
from bot.arbitrage.executor import ArbitrageExecutor, LiveExecutionNotAllowedError
from bot.arbitrage.models import ArbKind, Leg, Opportunity
from bot.arbitrage.service import ArbitrageService, arbitrage_symbols
from bot.config import Mode
from bot.config.schema import AppConfig, ArbitrageConfig, CrossExchangeConfig
from bot.core.clock import ManualClock
from bot.core.event_bus import EventBus
from bot.core.events import OrderBookEvent
from bot.core.models import (
    MarketInfo,
    MarketPrecision,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderRequest,
    Side,
)
from bot.exchanges.errors import ExchangeNetworkError
from bot.exchanges.paper import PaperExchange
from bot.risk.kill_switch import KillReason, KillSwitch
from bot.risk.manager import RiskManager
from bot.risk.portfolio import PortfolioView
from bot.risk.sizing import GrowthSizer
from bot.storage.repository import Repository

T = datetime(2026, 1, 1, tzinfo=UTC)
PREC = MarketPrecision(tick_size=D("0.01"), lot_size=D("0.0001"), min_notional=D(1))


def info(ex: str, sym: str) -> MarketInfo:
    b, q = sym.split("/")
    return MarketInfo(
        exchange=ex,
        symbol=sym,
        base=b,
        quote=q,
        precision=PREC,
        maker_fee=D("0.001"),
        taker_fee=D("0.001"),
    )


def ob(
    ex: str, sym: str, bid: str, ask: str, bid_amt: str = "10", ask_amt: str = "10"
) -> OrderBook:
    return OrderBook(
        exchange=ex,
        symbol=sym,
        timestamp=T,
        bids=(OrderBookLevel(price=D(bid), amount=D(bid_amt)),),
        asks=(OrderBookLevel(price=D(ask), amount=D(ask_amt)),),
    )


async def paper(name: str, books: list[OrderBook], **bal: str) -> PaperExchange:
    syms = {b.symbol for b in books}
    ex = PaperExchange(
        name,
        initial_balances={k: D(v) for k, v in bal.items()},
        markets={s: info(name, s) for s in syms},
        clock=ManualClock(T),
    )
    for b in books:
        await ex.process_order_book(b)
    return ex


def risk(cfg: AppConfig | None = None) -> RiskManager:
    cfg = cfg or AppConfig.model_validate(
        {"risk": {"max_exposure_per_symbol_pct": 50, "max_total_exposure_pct": 100}}
    )
    return RiskManager(cfg, KillSwitch(cfg.risk, ManualClock(T)), GrowthSizer(cfg.risk, D(10_000)))


def pv() -> PortfolioView:
    return PortfolioView(D(10_000))


async def cross_world(sell_depth: str = "10") -> tuple[PaperExchange, PaperExchange, Opportunity]:
    a = await paper("A", [ob("A", "BTC/USDT", "99", "100")], USDT="1000")
    b = await paper("B", [ob("B", "BTC/USDT", "101", "102", bid_amt=sell_depth)], BTC="5")
    cfg = CrossExchangeConfig(enabled=True, pairs=("BTC/USDT",), max_notional_quote=D(200))
    opp = evaluate_pair(
        Venue("A", "BTC/USDT", ob("A", "BTC/USDT", "99", "100"), D("0.001")),
        Venue("B", "BTC/USDT", ob("B", "BTC/USDT", "101", "102"), D("0.001")),
        cfg,
        T,
    )
    assert opp is not None and opp.legs[0].amount == D(2)
    return a, b, opp


async def test_cross_both_legs_fill() -> None:
    a, b, opp = await cross_world()
    ex = ArbitrageExecutor({"A": a, "B": b}, risk(), Mode.PAPER, pv)
    res = await ex.execute(opp)
    assert res.status == "completed" and res.hedges == []
    assert (await a.fetch_balance())["BTC"].free == D(2)
    assert (await b.fetch_balance())["BTC"].free == D(3)
    assert (await b.fetch_balance())["USDT"].free == D(202) - D(202) * D("0.001")


async def test_sell_leg_partial_is_hedged_on_buy_venue() -> None:
    a, b, opp = await cross_world(sell_depth="0.5")  # book thinned after the scan
    ex = ArbitrageExecutor({"A": a, "B": b}, risk(), Mode.PAPER, pv)
    res = await ex.execute(opp)
    assert res.status == "hedged"
    assert res.hedges[0].side is Side.SELL and res.hedges[0].filled == D("1.5")
    # net inventory is flat: A keeps the 0.5 BTC that matches the 0.5 sold on B
    assert (await a.fetch_balance())["BTC"].free == D("0.5")
    assert (await b.fetch_balance())["BTC"].free == D("4.5")


async def test_buy_leg_failure_buys_back_on_sell_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    a, b, opp = await cross_world()

    async def boom(req: OrderRequest) -> Order:
        raise ExchangeNetworkError("A down")

    monkeypatch.setattr(a, "create_order", boom)
    ex = ArbitrageExecutor({"A": a, "B": b}, risk(), Mode.PAPER, pv)
    res = await ex.execute(opp)
    assert res.status == "hedged"
    assert res.hedges[0].exchange == "B" and res.hedges[0].side is Side.BUY
    # inventory restored as far as the (fee-reduced) sale proceeds allow
    # sold 2 @101 (-0.1% fee), bought back @102 (+0.1%): 201.798 / 102.102 = 1.9764
    assert (await b.fetch_balance())["BTC"].free == D("4.9764")
    assert any("A bacağı hata" in r for r in res.reasons)


async def test_no_fill_aborts() -> None:
    a, b, opp = await cross_world()
    await a.process_order_book(ob("A", "BTC/USDT", "99", "150"))  # price ran away
    await b.process_order_book(ob("B", "BTC/USDT", "50", "102"))
    res = await ArbitrageExecutor({"A": a, "B": b}, risk(), Mode.PAPER, pv).execute(opp)
    assert res.status == "aborted" and res.hedges == []


async def test_kill_switch_rejects_new_legs() -> None:
    a, b, opp = await cross_world()
    r = risk()
    await r.kill_switch.trigger(KillReason.MANUAL, "x")
    res = await ArbitrageExecutor({"A": a, "B": b}, r, Mode.PAPER, pv).execute(opp)
    assert res.status == "rejected" and res.orders == []


def test_live_execution_refused() -> None:
    for mode in (Mode.LIVE, Mode.TESTNET):
        with pytest.raises(LiveExecutionNotAllowedError):
            ArbitrageExecutor({}, risk(), mode, pv)


def tri_opp() -> Opportunity:
    legs = (
        Leg("X", "BTC/USDT", Side.BUY, D(1), D(100), D(100)),
        Leg("X", "ETH/BTC", Side.BUY, D(20), D("0.05"), D("0.05")),
        Leg("X", "ETH/USDT", Side.SELL, D(20), D("5.1"), D("5.1")),
    )
    return Opportunity(
        ArbKind.TRIANGULAR, "tri", legs, D(2), D("1.7"), D(100), D("1.7"), False, 0.0, T
    )


async def tri_exchange(eth_btc_ask: str = "0.05") -> PaperExchange:
    return await paper(
        "X",
        [
            ob("X", "BTC/USDT", "99.9", "100"),
            ob("X", "ETH/BTC", "0.0499", eth_btc_ask, "100", "100"),
            ob("X", "ETH/USDT", "5.1", "5.2", "100", "100"),
        ],
        USDT="1000",
    )


async def test_triangular_completes() -> None:
    x = await tri_exchange()
    res = await ArbitrageExecutor({"X": x}, risk(), Mode.PAPER, pv).execute(tri_opp())
    assert res.status == "completed" and len(res.orders) == 3
    bal = await x.fetch_balance()
    assert bal["USDT"].free > D(1000)
    assert bal.get("BTC") is None or bal["BTC"].total < D("0.01")


async def test_triangular_middle_leg_failure_unwinds_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    x = await tri_exchange()
    real = x.create_order

    async def fail_eth_btc(req: OrderRequest) -> Order:
        if req.symbol == "ETH/BTC":
            raise ExchangeNetworkError("leg 2 down")
        return await real(req)

    monkeypatch.setattr(x, "create_order", fail_eth_btc)
    res = await ArbitrageExecutor({"X": x}, risk(), Mode.PAPER, pv).execute(tri_opp())
    assert res.status == "hedged"
    assert res.hedges[0].symbol == "BTC/USDT" and res.hedges[0].side is Side.SELL
    bal = await x.fetch_balance()
    assert bal.get("BTC") is None  # back in USDT (minus fees/spread)
    assert D(990) < bal["USDT"].free < D(1000)


# --------------------------------------------------------------------------- service


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repository]:
    r = await Repository.connect(f"sqlite+aiosqlite:///{tmp_path / 'arb.db'}")
    yield r
    await r.close()


async def test_service_logs_and_executes_in_paper(repo: Repository) -> None:
    a = await paper("A", [ob("A", "BTC/USDT", "99", "100")], USDT="1000")
    b = await paper("B", [ob("B", "BTC/USDT", "101", "102")], BTC="5")
    cfg = ArbitrageConfig(
        cross_exchange=CrossExchangeConfig(
            enabled=True, pairs=("BTC/USDT",), max_notional_quote=D(200)
        ),
        min_interval_seconds=10,
    )
    bus = EventBus()
    clock_val = [0.0]
    ex = ArbitrageExecutor({"A": a, "B": b}, risk(), Mode.PAPER, pv)
    svc = ArbitrageService(
        cfg, bus, {"A": a, "B": b}, repo, ex, ManualClock(T), monotonic=lambda: clock_val[0]
    )
    await svc.on_book(OrderBookEvent(order_book=ob("A", "BTC/USDT", "99", "100")))
    assert svc.found == []  # one venue only
    await svc.on_book(OrderBookEvent(order_book=ob("B", "BTC/USDT", "101", "102")))
    await svc.on_book(OrderBookEvent(order_book=ob("B", "BTC/USDT", "101", "102")))
    assert len(svc.found) == 1  # deduplicated within min_interval
    assert (await a.fetch_balance())["BTC"].free == D(2)  # executed on paper


async def test_service_log_only_without_executor() -> None:
    a = await paper("A", [ob("A", "BTC/USDT", "99", "100")], USDT="1000")
    b = await paper("B", [ob("B", "BTC/USDT", "101", "102")], BTC="5")
    cfg = ArbitrageConfig(cross_exchange=CrossExchangeConfig(enabled=True, pairs=("BTC/USDT",)))
    svc = ArbitrageService(cfg, EventBus(), {"A": a, "B": b}, None, None, ManualClock(T))
    for book in (ob("A", "BTC/USDT", "99", "100"), ob("B", "BTC/USDT", "101", "102")):
        await svc.on_book(OrderBookEvent(order_book=book))
    assert len(svc.found) == 1
    assert (await a.fetch_balance()).get("BTC") is None


def test_watched_symbols_include_fx_and_triangle() -> None:
    cfg = ArbitrageConfig.model_validate(
        {
            "cross_exchange": {
                "enabled": True,
                "pairs": ["BTC/USDT"],
                "fx_symbols": {"btcturk": "USDT/TRY"},
            },
            "triangular": {
                "enabled": True,
                "exchange": "binance",
                "symbols": ["BTC/USDT", "ETH/BTC", "ETH/USDT"],
            },
        }
    )
    assert arbitrage_symbols(cfg, "btcturk") == {"BTC/TRY", "USDT/TRY"}
    assert arbitrage_symbols(cfg, "binance") == {"BTC/USDT", "ETH/BTC", "ETH/USDT"}
