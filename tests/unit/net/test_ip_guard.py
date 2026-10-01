"""IP mismatch / proxy loss must block orders (fail-closed)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bot.core.clock import ManualClock
from bot.core.events import AlertLevel, RiskAlert
from bot.net.errors import EgressBlockedError, EgressUnavailableError
from bot.net.ip_guard import IpGuard, IpStatus

EXPECTED = "8.8.8.8"
SERVICES = ("https://a.example", "https://b.example")


class FakeIpServices:
    def __init__(self) -> None:
        self.answers: dict[str, str | Exception] = {s: EXPECTED for s in SERVICES}
        self.calls = 0

    async def __call__(self, url: str) -> str:
        self.calls += 1
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


def make_guard(now: datetime) -> tuple[IpGuard, FakeIpServices, ManualClock, list[RiskAlert]]:
    fake = FakeIpServices()
    clock = ManualClock(now)
    alerts: list[RiskAlert] = []

    async def sink(a: RiskAlert) -> None:
        alerts.append(a)

    guard = IpGuard(EXPECTED, SERVICES, fake, timedelta(minutes=5), clock, sink)
    return guard, fake, clock, alerts


async def test_blocked_before_first_check(now: datetime) -> None:
    guard, *_ = make_guard(now)
    assert guard.status is IpStatus.UNKNOWN
    assert not guard.allows_orders()
    with pytest.raises(EgressBlockedError):
        guard.ensure_orders_allowed()


async def test_ok_allows_orders(now: datetime) -> None:
    guard, fake, _, alerts = make_guard(now)
    result = await guard.check()
    assert result.ok
    assert fake.calls == 2  # both services queried
    guard.ensure_orders_allowed()
    assert alerts == []  # first success is not an alert


async def test_ip_mismatch_blocks_and_alerts(now: datetime) -> None:
    guard, fake, _, alerts = make_guard(now)
    await guard.check()
    fake.answers = {s: "1.1.1.1" for s in SERVICES}
    result = await guard.check()
    assert result.status is IpStatus.MISMATCH
    assert not guard.allows_orders()
    with pytest.raises(EgressBlockedError):
        guard.ensure_orders_allowed()
    assert len(alerts) == 1
    assert alerts[0].level is AlertLevel.CRITICAL
    assert alerts[0].code == "egress_ip_mismatch"
    assert alerts[0].details["observed"] == {s: "1.1.1.1" for s in SERVICES}


async def test_single_service_mismatch_is_inconsistent(now: datetime) -> None:
    guard, fake, _, alerts = make_guard(now)
    fake.answers[SERVICES[1]] = "1.1.1.1"
    assert (await guard.check()).status is IpStatus.INCONSISTENT
    assert not guard.allows_orders()
    assert alerts[0].code == "egress_ip_inconsistent"


async def test_proxy_down_blocks(now: datetime) -> None:
    guard, fake, _, alerts = make_guard(now)
    await guard.check()
    fake.answers[SERVICES[0]] = EgressUnavailableError("proxy down")
    result = await guard.check()
    assert result.status is IpStatus.UNREACHABLE
    assert SERVICES[0] in result.errors
    assert not guard.allows_orders()
    assert alerts[-1].code == "egress_ip_unreachable"


async def test_one_service_down_still_blocks(now: datetime) -> None:
    """A single reachable service matching is not enough: both must confirm."""
    guard, fake, _, _ = make_guard(now)
    fake.answers[SERVICES[1]] = TimeoutError()
    assert (await guard.check()).status is IpStatus.UNREACHABLE
    assert not guard.allows_orders()


async def test_garbage_response_blocks(now: datetime) -> None:
    guard, fake, _, _ = make_guard(now)
    fake.answers[SERVICES[0]] = "<html>captive portal</html>"
    assert (await guard.check()).status is IpStatus.UNREACHABLE
    assert not guard.allows_orders()


async def test_stale_check_blocks(now: datetime) -> None:
    guard, _, clock, _ = make_guard(now)
    await guard.check()
    clock.advance(timedelta(minutes=9))
    assert guard.allows_orders()
    clock.advance(timedelta(minutes=2))
    assert guard.is_stale()
    assert not guard.allows_orders()


async def test_recovery_alert_and_no_duplicate_alerts(now: datetime) -> None:
    guard, fake, _, alerts = make_guard(now)
    fake.answers = {s: "1.1.1.1" for s in SERVICES}
    await guard.check()
    await guard.check()  # same failure: no duplicate alert
    assert len(alerts) == 1
    fake.answers = {s: EXPECTED for s in SERVICES}
    await guard.check()
    assert guard.allows_orders()
    assert [a.code for a in alerts] == ["egress_ip_mismatch", "egress_ip_recovered"]
    assert alerts[-1].level is AlertLevel.INFO


async def test_ip_normalization(now: datetime) -> None:
    guard, fake, _, _ = make_guard(now)
    fake.answers = {s: f" {EXPECTED}\n" for s in SERVICES}
    assert (await guard.check()).ok


def test_requires_two_services(now: datetime) -> None:
    async def f(_: str) -> str:
        return EXPECTED

    with pytest.raises(ValueError):
        IpGuard(EXPECTED, ["https://a"], f, timedelta(minutes=1))
    with pytest.raises(ValueError):
        IpGuard(EXPECTED, ["https://a", "https://a"], f, timedelta(minutes=1))


async def test_run_periodic_survives_crash_and_stops(now: datetime) -> None:
    calls = 0

    async def flaky(_: str) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("unexpected")  # not an egress error -> crash path

    guard = IpGuard(EXPECTED, SERVICES, flaky, timedelta(seconds=0.01))
    stop = asyncio.Event()
    task = asyncio.create_task(guard.run_periodic(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert calls >= 2
    assert not guard.allows_orders()
