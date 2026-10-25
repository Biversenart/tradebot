"""ExecutionEngine + reconciliation against PaperExchange ('testnet scenario')."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from bot.config.schema import RiskConfig
from bot.core.clock import ManualClock
from bot.core.events import RiskAlert
from bot.core.models import (
    MarketInfo,
    MarketPrecision,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from bot.exchanges.errors import ExchangeNetworkError, InvalidOrderError
from bot.exchanges.paper import PaperExchange
from bot.execution.engine import ExecutionConfig, ExecutionEngine, client_order_id
from bot.execution.reconcile import reconcile
from bot.risk import manager as rm
from bot.risk.kill_switch import KillSwitch
from bot.storage.repository import Repository

T = datetime(2026, 1, 1, tzinfo=UTC)
SYM = "BTC/USDT"
INFO = MarketInfo(
    exchange="paper",
    symbol=SYM,
    base="BTC",
    quote="USDT",
    precision=MarketPrecision(tick_size=D("0.01"), lot_size=D("0.001"), min_notional=D(5)),
    maker_fee=D("0.001"),
    taker_fee=D("0.001"),
)


def book(bid: str, ask: str, depth: str = "50") -> OrderBook:
    return OrderBook(
        exchange="paper",
        symbol=SYM,
        timestamp=T,
        bids=(OrderBookLevel(price=D(bid), amount=D(depth)),),
        asks=(OrderBookLevel(price=D(ask), amount=D(depth)),),
    )


def approve(intent: OrderIntent) -> rm.ApprovedIntent:
    return rm.ApprovedIntent(intent, None, T, _token=rm._TOKEN)


def entry(amount: str = "1", stop: str = "95") -> rm.ApprovedIntent:
    return approve(
        OrderIntent(
            exchange="paper",
            symbol=SYM,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            amount=D(amount),
            stop_loss=D(stop),
            strategy="s",
        )
    )


def exit_intent(amount: str) -> rm.ApprovedIntent:
    return approve(
        OrderIntent(
            exchange="paper",
            symbol=SYM,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            amount=D(amount),
            reduce_only=True,
            strategy="s",
        )
    )


class Env:
    def __init__(self, ex: PaperExchange, repo: Repository, alerts: list[RiskAlert]) -> None:
        self.ex, self.repo, self.alerts = ex, repo, alerts
        self.ks = KillSwitch(RiskConfig(), ManualClock(T))
        self.engine = self.make_engine()

    def make_engine(self) -> ExecutionEngine:
        async def sink(a: RiskAlert) -> None:
            self.alerts.append(a)

        cfg = ExecutionConfig(
            order_timeout_seconds=0.2, poll_interval_seconds=0.01, retry_backoff_seconds=0
        )
        return ExecutionEngine(self.ex, self.repo, self.ks, cfg, ManualClock(T), sink)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    ex = PaperExchange(
        "paper", initial_balances={"USDT": D(10_000)}, markets={SYM: INFO}, clock=ManualClock(T)
    )
    await ex.process_order_book(book("99.9", "100"))
    repo = await Repository.connect(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    yield Env(ex, repo, [])
    await repo.close()


def test_client_order_id_deterministic_and_valid() -> None:
    a = client_order_id("intent-1", "entry")
    assert a == client_order_id("intent-1", "entry") != client_order_id("intent-1", "stop")
    OrderRequest(
        client_order_id=a,
        exchange="x",
        symbol=SYM,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        amount=D(1),
    )  # passes the id pattern
    assert len(a) <= 36


async def test_open_position_places_exchange_stop(env: Env) -> None:
    pos = await env.engine.open_position(entry(), [D(105), D(110)])
    assert pos is not None and pos.amount == D(1) and pos.entry_price == D(100)
    assert pos.stop_order_id is not None
    stop = await env.ex.fetch_order_by_client_id(pos.stop_order_id, SYM)
    assert stop.order_type is OrderType.STOP_MARKET and stop.status is OrderStatus.OPEN
    assert stop.stop_price == D(95) and stop.amount == D(1)
    assert len(await env.repo.open_positions()) == 1
    assert {o.role for o in await env.repo.open_orders()} == {"stop"}


async def test_stop_failure_triggers_emergency_close(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = env.ex.create_order

    async def reject_stops(req: OrderRequest) -> Order:
        if req.order_type is OrderType.STOP_MARKET:
            raise InvalidOrderError("stop not allowed")
        return await real(req)

    monkeypatch.setattr(env.ex, "create_order", reject_stops)
    pos = await env.engine.open_position(entry(), [D(105)])
    assert pos is None
    p = (await env.repo.closed_positions())[0]
    assert p.close_reason == "stop_failed" and p.amount == 0
    assert any(a.code == "emergency_close_stop_failed" for a in env.alerts)
    assert (await env.ex.fetch_balance()).get("BTC") is None  # nothing left unprotected


async def test_reduce_moves_stop_and_books_pnl(env: Env) -> None:
    pos = await env.engine.open_position(entry(), [D(105), D(110)])
    assert pos is not None
    await env.ex.process_order_book(book("105", "105.1"))
    pnl = await env.engine.reduce(pos, exit_intent("0.4"), "tp1", new_stop=D(100))
    assert pnl > 0 and pos.amount == D("0.6") and pos.stop_loss == D(100)
    stop = await env.ex.fetch_order_by_client_id(pos.stop_order_id or "", SYM)
    assert stop.amount == D("0.6") and stop.status is OrderStatus.OPEN
    # the previous stop was cancelled (only one live stop)
    opens = [o for o in await env.ex.fetch_open_orders(SYM)]
    assert len(opens) == 1


async def test_exchange_stop_fill_is_detected(env: Env) -> None:
    pos = await env.engine.open_position(entry(), [D(105)])
    assert pos is not None
    await env.ex.process_order_book(book("94", "94.1"))  # stop triggers on the exchange
    assert await env.engine.sync_stop(pos) is True
    assert pos.status == "closed" and pos.close_reason == "stop"
    assert pos.realized_pnl < 0


async def test_retry_is_idempotent(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real = env.ex.create_order
    calls = {"n": 0}

    async def flaky(req: OrderRequest) -> Order:
        calls["n"] += 1
        order = await real(req)  # reaches the exchange...
        if calls["n"] == 1:
            raise ExchangeNetworkError("timeout after send")  # ...but the reply is lost
        return order

    monkeypatch.setattr(env.ex, "create_order", flaky)
    pos = await env.engine.open_position(entry(), [D(105)])
    assert pos is not None and pos.amount == D(1)  # not doubled
    assert (await env.ex.fetch_balance())["BTC"].total == D(1)
    assert env.ks.state.consecutive_errors == 0  # success resets the counter


async def test_errors_feed_kill_switch(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(req: OrderRequest) -> Order:
        raise ExchangeNetworkError("down")

    async def not_found(cid: str, symbol: str) -> Order:
        from bot.exchanges.errors import OrderNotFoundError

        raise OrderNotFoundError(cid)

    monkeypatch.setattr(env.ex, "create_order", down)
    monkeypatch.setattr(env.ex, "fetch_order_by_client_id", not_found)
    with pytest.raises(ExchangeNetworkError):
        await env.engine.open_position(entry(), [D(105)])
    assert env.ks.state.consecutive_errors == 4  # 1 + 3 retries


async def test_limit_entry_timeout_keeps_partial(env: Env) -> None:
    await env.ex.process_order_book(book("99.9", "100", depth="0.3"))
    limit = approve(
        OrderIntent(
            exchange="paper",
            symbol=SYM,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            amount=D(1),
            price=D(100),
            stop_loss=D(95),
            strategy="s",
        )
    )
    pos = await env.engine.open_position(limit, [D(105)])
    assert pos is not None and pos.amount == D("0.3")  # partial fill, remainder cancelled
    rows = await env.repo.open_orders()
    assert [r.role for r in rows] == ["stop"]


# --------------------------------------------------------------------------- reconciliation


async def restart(env: Env) -> Env:
    """Simulate a crash/restart: new engine + repository on the same DB and exchange."""
    repo2 = Repository(env.repo.engine)
    new = Env(env.ex, repo2, env.alerts)
    return new


async def test_reconcile_replaces_stop_cancelled_while_down(env: Env) -> None:
    pos = await env.engine.open_position(entry(), [D(105)])
    assert pos is not None and pos.stop_order_id
    await env.ex.cancel_order(pos.stop_order_id, SYM)  # someone cancelled it while we were down
    new = await restart(env)
    rep = await reconcile(new.engine)
    assert pos.position_id in rep.stops_replaced
    p = await new.repo.get_position(pos.position_id)
    assert p is not None and p.stop_order_id != pos.stop_order_id
    stop = await env.ex.fetch_order_by_client_id(p.stop_order_id or "", SYM)
    assert stop.status is OrderStatus.OPEN


async def test_reconcile_closes_position_stopped_while_down(env: Env) -> None:
    pos = await env.engine.open_position(entry(), [D(105)])
    assert pos is not None
    await env.ex.process_order_book(book("93", "93.1"))  # stop fills while the bot is down
    new = await restart(env)
    rep = await reconcile(new.engine)
    assert rep.positions_closed == [pos.position_id]
    p = await new.repo.get_position(pos.position_id)
    assert p is not None and p.status == "closed"


async def test_reconcile_adopts_own_orphans_and_ignores_foreign(env: Env) -> None:
    for cid, px in (("tbdeadbeef", "90"), ("manual-123", "80")):
        await env.ex.create_order(
            OrderRequest(
                client_order_id=cid,
                exchange="paper",
                symbol=SYM,
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                amount=D("0.1"),
                price=D(px),
            )
        )
    rep = await reconcile(env.engine)
    assert rep.adopted_orders == ["tbdeadbeef"] and rep.foreign_orders == ["manual-123"]
    assert (await env.ex.fetch_order_by_client_id("manual-123", SYM)).status is OrderStatus.OPEN


async def test_reconcile_detects_missing_balance(env: Env) -> None:
    pos = await env.engine.open_position(entry(), [D(105)])
    assert pos is not None and pos.stop_order_id
    # manual sell on the exchange UI: cancel our stop and dump the coins
    await env.ex.cancel_order(pos.stop_order_id, SYM)
    await env.ex.create_order(
        OrderRequest(
            client_order_id="manual-sell",
            exchange="paper",
            symbol=SYM,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            amount=D(1),
        )
    )
    rep = await reconcile((await restart(env)).engine)
    assert pos.position_id in rep.positions_adjusted and pos.position_id in rep.positions_closed


async def test_reconcile_noop_when_consistent(env: Env) -> None:
    pos = await env.engine.open_position(entry(), [D(105)])
    assert pos is not None
    rep = await reconcile((await restart(env)).engine)
    assert not rep.changed and rep.errors == []


@pytest.mark.integration
async def test_reconcile_on_binance_testnet() -> None:  # pragma: no cover - needs testnet keys
    """Real testnet scenario (run manually: pytest -m integration with BINANCE_TESTNET_* set)."""
    import os

    if not os.environ.get("BINANCE_TESTNET_API_KEY"):
        pytest.skip("BINANCE_TESTNET_API_KEY yok")
    from bot.exchanges.binance import BinanceAdapter

    ad = BinanceAdapter.create(
        market_type="spot",
        testnet=True,
        proxy_options={},
        api_key=os.environ["BINANCE_TESTNET_API_KEY"],
        secret=os.environ["BINANCE_TESTNET_API_SECRET"],
    )
    async with ad:
        repo = await Repository.connect("sqlite+aiosqlite:///:memory:")
        eng = ExecutionEngine(ad, repo, KillSwitch(RiskConfig()))
        rep = await reconcile(eng)
        assert rep.errors == [] or all("bulunamadı" in e for e in rep.errors)
        await repo.close()
