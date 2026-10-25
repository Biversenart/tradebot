"""Chaos: the egress proxy / tunnel dies mid-session (CLAUDE.md rule 8, fail-closed)."""

from __future__ import annotations

from datetime import timedelta

from bot.config import EgressMode
from bot.core.events import SignalEvent
from bot.net.egress import Egress
from bot.net.errors import EgressUnavailableError
from bot.net.ip_guard import IpGuard
from bot.risk.kill_switch import KillReason
from tests.chaos.conftest import ChaosWorld
from tests.unit.execution.test_coordinator import signal
from tests.unit.net.test_egress import ServerFactory, free_port, settings_for
from tests.unit.net.test_egress import start_server as start_server

EXPECTED = "8.8.8.8"
SERVICES = ("https://a.example", "https://b.example")


class Proxy:
    """IP services as seen through the proxy; `up = False` simulates the proxy dying."""

    def __init__(self) -> None:
        self.up = True

    async def __call__(self, url: str) -> str:
        if not self.up:
            raise EgressUnavailableError("proxy bağlantısı yok")
        return EXPECTED


async def test_proxy_dies_orders_stop_then_resume(chaos: ChaosWorld) -> None:
    proxy = Proxy()
    guard = IpGuard(
        EXPECTED, SERVICES, proxy, timedelta(minutes=5), chaos.clock, chaos.coord.on_alert
    )
    chaos.risk.order_gate = guard.allows_orders
    assert not guard.allows_orders()  # nothing verified yet: closed
    await guard.check()
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    assert len(await chaos.repo.open_positions()) == 1

    proxy.up = False
    await guard.check()
    assert not guard.allows_orders()
    assert KillReason.EGRESS_IP in chaos.ks.active_reasons
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    assert len(await chaos.repo.open_positions()) == 1  # no new order while down
    # the existing position keeps its exchange-side stop (nothing to do from the bot)
    [pos] = await chaos.repo.open_positions()
    assert pos.stop_order_id is not None

    proxy.up = True
    await guard.check()
    assert guard.allows_orders() and KillReason.EGRESS_IP not in chaos.ks.active_reasons
    events = [e.code for e in await chaos.repo.recent_risk_events(10)]
    assert "egress_ip_unreachable" in events and "egress_ip_recovered" in events


async def test_stale_verification_closes_the_gate(chaos: ChaosWorld) -> None:
    guard = IpGuard(EXPECTED, SERVICES, Proxy(), timedelta(minutes=5), chaos.clock)
    await guard.check()
    assert guard.allows_orders()
    chaos.clock.advance(timedelta(minutes=11))  # checker hung / died: result is stale
    assert not guard.allows_orders()


async def test_dead_proxy_never_reaches_target_directly(start_server: ServerFactory) -> None:
    target = await start_server("binance")
    eg = Egress.from_settings(settings_for(EgressMode.PROXY, f"socks5://127.0.0.1:{free_port()}"))
    async with eg.new_http_session() as http:
        for _ in range(5):  # retries keep failing closed
            try:
                await http.get_text(f"http://127.0.0.1:{target.port}/api/v3/order")
            except EgressUnavailableError:
                continue
            raise AssertionError("istek proxy olmadan gitti")
    assert target.hits == []
    opts = eg.ccxt_options()  # ccxt REST + WS bound to the same (dead) proxy
    assert set(opts) == {"socksProxy", "wsSocksProxy"}
