from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import inspect

from bot.core.events import AlertLevel, RiskAlert
from bot.core.models import Fill, Order, OrderStatus, OrderType, Side
from bot.storage.models import Base, PositionRow
from bot.storage.repository import Repository

T = datetime(2026, 1, 1, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repository]:
    r = await Repository.connect(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    yield r
    await r.close()


def order(status: OrderStatus = OrderStatus.OPEN, filled: str = "0") -> Order:
    return Order(
        client_order_id="tb1",
        exchange_order_id="9",
        exchange="paper",
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        amount=D("0.123456789012345678"),
        price=D("100.10"),
        status=status,
        filled=D(filled),
        created_at=T,
    )


async def test_order_upsert_and_decimal_exactness(repo: Repository) -> None:
    await repo.save_order(order(), "entry", "p1", "s")
    assert [r.client_order_id for r in await repo.open_orders("paper")] == ["tb1"]
    await repo.save_order(order(OrderStatus.FILLED, "0.123456789012345678"), "entry")
    row = await repo.get_order("tb1")
    assert row is not None and row.status == "filled" and row.position_id == "p1"
    assert row.amount == D("0.123456789012345678")  # exact through SQLite
    assert row.created_at.tzinfo is not None
    assert await repo.open_orders() == []


async def test_fill_dedup(repo: Repository) -> None:
    f = Fill(
        trade_id="t1",
        client_order_id="tb1",
        exchange="paper",
        symbol="BTC/USDT",
        side=Side.BUY,
        price=D(100),
        amount=D(1),
        timestamp=T,
    )
    assert await repo.save_fill(f) is True
    assert await repo.save_fill(f) is False


async def test_positions_state_events_equity(repo: Repository) -> None:
    pos = PositionRow(
        position_id="p1",
        exchange="paper",
        symbol="BTC/USDT",
        side="long",
        strategy="s",
        amount=D(1),
        initial_amount=D(1),
        entry_price=D(100),
        initial_stop=D(95),
        stop_loss=D(95),
        take_profits=["105"],
        status="open",
        opened_at=T,
    )
    await repo.save_position(pos)
    assert [p.position_id for p in await repo.open_positions()] == ["p1"]
    pos.status, pos.closed_at = "closed", T
    await repo.save_position(pos)
    assert await repo.open_positions() == [] and len(await repo.closed_positions()) == 1
    await repo.set_state("kill_switch", {"reasons": {"manual": "x"}})
    assert await repo.get_state("kill_switch") == {"reasons": {"manual": "x"}}
    assert await repo.get_state("nope") is None
    await repo.save_risk_event(
        RiskAlert(level=AlertLevel.CRITICAL, code="c", message="m", details={"d": D(1)})
    )
    assert (await repo.recent_risk_events())[0].code == "c"
    await repo.save_equity(D("10000.5"))
    assert (await repo.equity_curve())[0].equity == D("10000.5")


async def test_naive_datetime_rejected(repo: Repository) -> None:
    pos = PositionRow(
        position_id="p2",
        exchange="paper",
        symbol="X",
        side="long",
        strategy="s",
        amount=D(1),
        initial_amount=D(1),
        entry_price=D(1),
        initial_stop=D(1),
        stop_loss=D(1),
        status="open",
        opened_at=datetime(2026, 1, 1),
    )
    with pytest.raises(Exception, match="Zaman dilimi"):
        await repo.save_position(pos)


def test_alembic_migration_matches_models(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    env = {"ALEMBIC_DATABASE_URL": f"sqlite+aiosqlite:///{db}", "PATH": ""}
    alembic = [sys.executable, "-m", "alembic"]
    subprocess.run(
        [*alembic, "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    check = subprocess.run(
        [*alembic, "check"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    from sqlalchemy import create_engine

    eng = create_engine(f"sqlite:///{db}")
    tables = set(inspect(eng).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    for name in (
        "orders",
        "trades",
        "positions",
        "signals",
        "risk_reports",
        "balances_snapshot",
        "arbitrage_opportunities",
        "risk_events",
        "equity_curve",
        "config_changes",
    ):
        assert name in tables  # spec §5.8
