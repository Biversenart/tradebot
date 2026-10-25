from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from bot.config.schema import AppConfig, Mode
from bot.core.clock import ManualClock
from bot.core.events import RiskAlert
from bot.core.models import MarketInfo, MarketPrecision
from bot.exchanges.paper import PaperExchange
from bot.ops.announcements import Announcement, AnnouncementKind
from bot.ops.service import OpsService
from tests.unit.execution.test_coordinator import SYM, T, World, book, signal
from tests.unit.execution.test_coordinator import world as world


class Static:
    name = "static"

    def __init__(self, items: list[Announcement]) -> None:
        self.items = items
        self.calls = 0

    async def fetch(self) -> list[Announcement]:
        self.calls += 1
        return self.items


async def test_check_throttles_and_collects_prices() -> None:
    usdc = MarketInfo(
        exchange="paper",
        symbol="USDC/USDT",
        base="USDC",
        quote="USDT",
        precision=MarketPrecision(tick_size=D("0.0001"), lot_size=D(1), min_notional=D(1)),
    )
    clock = ManualClock(T)
    ex = PaperExchange("paper", markets={"USDC/USDT": usdc}, clock=clock)
    await ex.process_order_book(book("0.97", "0.971").model_copy(update={"symbol": "USDC/USDT"}))
    alerts: list[RiskAlert] = []

    async def sink(a: RiskAlert) -> None:
        alerts.append(a)

    src = Static([])
    cfg = AppConfig.model_validate({"operations": {"stablecoin_pairs": ["USDC/USDT"]}})
    svc = OpsService(cfg, Mode.PAPER, {"paper": ex}, sink, clock=clock, sources=[src])
    assert await svc.maybe_check({"paper": D(9000)})
    assert not await svc.maybe_check()  # throttled (check_interval_seconds)
    assert src.calls == 1
    assert svc.guard.block_reason("paper", SYM) is not None  # USDC at 0.97 -> depeg
    codes = {a.code for a in alerts}
    assert {"stablecoin_depeg", "exchange_balance_cap"} <= codes
    clock.advance(timedelta(seconds=61))
    assert await svc.maybe_check()


def test_default_sources(tmp_path: Path) -> None:
    async def get(_: str) -> str:
        return "{}"

    cfg = AppConfig.model_validate({"operations": {"announcements_file": str(tmp_path / "a.yaml")}})
    live = OpsService(cfg, Mode.LIVE, {"binance": PaperExchange("binance")}, http_get=get)
    assert live.announcements is not None
    assert [s.name for s in live.announcements.sources] == ["file", "binance_cms"]
    off = OpsService(
        AppConfig.model_validate({"operations": {"watch_exchange_announcements": False}}),
        Mode.LIVE,
        {},
    )
    assert off.announcements is None and off.guard.announcements is None


async def test_coordinator_closes_delisted_positions(world: World) -> None:
    src = Static([Announcement("d1", "paper", AnnouncementKind.DELISTING, "delist BTC", ("BTC",))])
    svc = OpsService(
        world.cfg, Mode.PAPER, {"paper": world.ex}, clock=ManualClock(T), sources=[src]
    )
    world.coord.ops = svc
    from bot.core.events import SignalEvent

    await world.coord.on_signal(SignalEvent(signal=signal()))
    assert len(await world.repo.open_positions()) == 1
    await world.coord.tick()
    assert await world.repo.open_positions() == []
    closed = await world.repo.closed_positions(5)
    assert closed[0].close_reason == "delisting"
    # and new entries in the pair are refused while the announcement stands
    world.risk.ops = svc.guard
    await world.coord.on_signal(SignalEvent(signal=signal()))
    assert await world.repo.open_positions() == []


async def test_keep_policy_leaves_positions(world: World) -> None:
    cfg = world.cfg.model_copy(
        update={"operations": world.cfg.operations.model_copy(update={"on_delisting": "keep"})}
    )
    src = Static([Announcement("d1", "paper", AnnouncementKind.DELISTING, "x", ("BTC",))])
    svc = OpsService(cfg, Mode.PAPER, {"paper": world.ex}, clock=ManualClock(T), sources=[src])
    world.coord.ops = svc
    from bot.core.events import SignalEvent

    await world.coord.on_signal(SignalEvent(signal=signal()))
    await world.coord.tick()
    assert len(await world.repo.open_positions()) == 1
