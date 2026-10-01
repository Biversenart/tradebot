from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import SecretStr

from bot.config import Mode, Settings
from bot.config.schema import AppConfig
from bot.config.secrets import Secrets
from bot.core.events import RiskAlert
from bot.runtime import BotRuntime

EXPECTED = "8.8.8.8"


def make_testnet_settings(**cfg: Any) -> Settings:
    config = AppConfig.model_validate({"mode": "testnet", "marketdata": {"enabled": False}, **cfg})
    secrets = Secrets(
        _env_file=None,
        trading_mode=Mode.TESTNET,
        egress_expected_ip=EXPECTED,
        heartbeat_url=SecretStr("https://hc.example/x"),
    )
    return Settings(config=config, secrets=secrets, mode=Mode.TESTNET)


def fetcher(ip: str) -> Any:
    async def fetch(_: str) -> str:
        return ip

    return fetch


async def start(rt: BotRuntime) -> tuple[asyncio.Event, asyncio.Task[None], list[RiskAlert]]:
    alerts: list[RiskAlert] = []

    async def on_alert(a: RiskAlert) -> None:
        alerts.append(a)

    rt.bus.subscribe(RiskAlert, on_alert)
    stop = asyncio.Event()
    task = asyncio.create_task(rt.run(stop))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if rt.ip_guard and rt.ip_guard.last_result is not None:
            break
    return stop, task, alerts


async def finish(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_paper_mode_has_no_gate() -> None:
    cfg = AppConfig.model_validate({"marketdata": {"enabled": False}})
    settings = Settings(cfg, Secrets(_env_file=None), Mode.PAPER)
    rt = BotRuntime(settings)
    assert rt.ip_guard is None
    assert rt.orders_allowed()


async def test_testnet_orders_blocked_on_ip_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = BotRuntime(make_testnet_settings(), ip_fetcher=fetcher("1.1.1.1"))
    called = False

    async def no_access_check() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(rt, "check_exchange_access", no_access_check)
    assert not rt.orders_allowed()  # blocked before the first check
    stop, task, alerts = await start(rt)
    assert not rt.orders_allowed()
    assert not called  # exchange is not contacted with an unverified IP
    await finish(stop, task)
    assert [a.code for a in alerts] == ["egress_ip_mismatch"]


async def test_testnet_orders_allowed_when_ip_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = BotRuntime(make_testnet_settings(), ip_fetcher=fetcher(EXPECTED))
    called = False

    async def access_check() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(rt, "check_exchange_access", access_check)
    stop, task, alerts = await start(rt)
    assert rt.orders_allowed()
    assert called
    await finish(stop, task)
    assert alerts == []


async def test_heartbeat_stops_when_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[str] = []

    async def pinger(url: str) -> int:
        pings.append(url)
        return 200

    rt = BotRuntime(
        make_testnet_settings(heartbeat={"enabled": True, "interval_seconds": 10}),
        ip_fetcher=fetcher("1.1.1.1"),
        heartbeat_pinger=pinger,
    )
    assert rt.heartbeat is not None
    stop, task, _ = await start(rt)
    await asyncio.sleep(0.05)
    await finish(stop, task)
    assert pings == []
    assert rt.heartbeat.skipped >= 1


async def test_feeds_not_started_when_egress_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig.model_validate({"mode": "testnet"})
    secrets = Secrets(_env_file=None, trading_mode=Mode.TESTNET, egress_expected_ip=EXPECTED)
    rt = BotRuntime(Settings(config, secrets, Mode.TESTNET), ip_fetcher=fetcher("1.1.1.1"))
    assert rt.feeds  # built (no network on construction)
    loaded: list[str] = []

    async def fake_load() -> None:
        loaded.append("x")

    for feed in rt.feeds:
        monkeypatch.setattr(feed.adapter, "load_markets", fake_load)
    stop, task, _ = await start(rt)
    await asyncio.sleep(0.02)
    await finish(stop, task)
    assert loaded == []  # exchange never contacted with an unverified IP
