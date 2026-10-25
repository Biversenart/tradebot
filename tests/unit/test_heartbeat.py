from __future__ import annotations

import asyncio
from datetime import timedelta

from pydantic import SecretStr

from bot.notify.heartbeat import Heartbeat

URL = SecretStr("https://hc-ping.com/secret-uuid")


async def test_ping_when_healthy() -> None:
    seen: list[str] = []

    async def ping(url: str) -> int:
        seen.append(url)
        return 200

    hb = Heartbeat(URL, timedelta(seconds=60), ping)
    assert await hb.beat()
    assert seen == ["https://hc-ping.com/secret-uuid"]
    assert hb.sent == 1


async def test_no_ping_when_unhealthy() -> None:
    async def ping(url: str) -> int:
        raise AssertionError("unhealthy bot must not ping")

    hb = Heartbeat(URL, timedelta(seconds=60), ping, is_healthy=lambda: False)
    assert not await hb.beat()
    assert hb.skipped == 1


async def test_failures_counted_not_raised() -> None:
    async def boom(url: str) -> int:
        raise OSError("down")

    async def bad_status(url: str) -> int:
        return 500

    hb = Heartbeat(URL, timedelta(seconds=60), boom)
    assert not await hb.beat()
    hb2 = Heartbeat(URL, timedelta(seconds=60), bad_status)
    assert not await hb2.beat()
    assert hb.failed == 1 and hb2.failed == 1


async def test_run_loop() -> None:
    count = 0

    async def ping(url: str) -> int:
        nonlocal count
        count += 1
        return 200

    hb = Heartbeat(URL, timedelta(seconds=0.01), ping)
    stop = asyncio.Event()
    task = asyncio.create_task(hb.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert count >= 2
