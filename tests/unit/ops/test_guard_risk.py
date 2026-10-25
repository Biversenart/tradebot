"""RiskManager + OpsGuard: §5.13 checks are applied to new risk, never to exits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from bot.config.schema import AppConfig, LowLiquidityConfig, Mode, OperationsConfig
from bot.core.clock import ManualClock
from bot.core.models import OrderIntent, OrderType, Side
from bot.ops.announcements import Announcement, AnnouncementKind, AnnouncementWatcher
from bot.ops.capital import CapitalCap
from bot.ops.clock_skew import ClockSkewMonitor
from bot.ops.guard import OpsGuard
from bot.ops.liquidity import LowLiquidityMode
from bot.ops.stablecoin import DepegMonitor
from bot.risk.kill_switch import KillSwitch
from bot.risk.manager import RiskManager
from bot.risk.portfolio import PortfolioView
from bot.risk.sizing import GrowthSizer
from tests.unit.risk.test_manager import EMPTY, book, ctx, sig

THU = datetime(2026, 1, 1, tzinfo=UTC)


def manager(guard: OpsGuard, now: datetime = THU) -> RiskManager:
    cfg = AppConfig.model_validate({"risk": {"growth": {"profit_lock": {"enabled": False}}}})
    ks = KillSwitch(cfg.risk, ManualClock(now))
    return RiskManager(cfg, ks, GrowthSizer(cfg.risk, D(10_000)), ops=guard)


async def test_depeg_and_clock_skew_block_entries() -> None:
    depeg = DepegMonitor(D("0.5"), ("USDC/USDT",))
    skew = ClockSkewMonitor(1000)
    rm = manager(OpsGuard(depeg=depeg, clock_skew=skew))
    assert rm.assess_signal(sig(), EMPTY, ctx(book=book())).ok
    await depeg.update({"USDC/USDT": D("0.97")})
    d = rm.assess_signal(sig(), EMPTY, ctx(book=book()))
    assert not d.ok and "depeg" in d.report.reasons[-1]
    await depeg.update({"USDC/USDT": D(1)})
    skew.skewed["binance"] = 5000
    d = rm.assess_signal(sig(), EMPTY, ctx(book=book()))
    assert not d.ok and "saat" in d.report.reasons[-1]


async def test_delisting_blocks_entries_and_arbitrage_but_not_exits() -> None:
    w = AnnouncementWatcher([], clock=ManualClock(THU))
    w.known["1"] = Announcement("1", "binance", AnnouncementKind.DELISTING, "t", ("BTC",))
    rm = manager(OpsGuard(announcements=w))
    assert not rm.assess_signal(sig(), EMPTY, ctx(book=book())).ok
    leg = OrderIntent(
        exchange="binance",
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        amount=D("0.1"),
        price=D(100),
    )
    assert not rm.assess_intent(leg, EMPTY, ctx()).ok
    exit_ = leg.model_copy(update={"side": Side.SELL, "reduce_only": True})
    assert rm.assess_exit(exit_, D(1)).ok


def test_live_capital_cap_shrinks_size() -> None:
    cap = CapitalCap(OperationsConfig(live_capital_cap_pct=D(10)), Mode.LIVE)
    d = manager(OpsGuard(capital=cap)).assess_signal(sig(), EMPTY, ctx(book=book()))
    assert d.ok and d.approved is not None
    assert d.approved.intent.amount == D(2)  # 1 % of 1 000 / 5 (vs 20 uncapped)
    assert any("kanarya" in r for r in d.report.reasons)
    # arbitrage legs are limited by capped equity too (60 % of 1 000)
    leg = OrderIntent(
        exchange="binance",
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        amount=D(7),
        price=D(100),
    )
    rm = manager(OpsGuard(capital=cap))
    assert not rm.assess_intent(leg, PortfolioView(equity=D(10_000)), ctx()).ok


def test_weekend_mode_reduces_or_halts() -> None:
    sat = THU + timedelta(days=2)
    rm = manager(OpsGuard(liquidity=LowLiquidityMode(LowLiquidityConfig())), sat)
    d = rm.assess_signal(sig(), EMPTY, ctx(book=book()))
    assert d.ok and d.approved is not None and d.approved.intent.amount == D(10)
    halt = LowLiquidityMode(LowLiquidityConfig(action="halt"))
    d = manager(OpsGuard(liquidity=halt), sat).assess_signal(sig(), EMPTY, ctx(book=book()))
    assert not d.ok and "düşük likidite" in d.report.reasons[-1]
