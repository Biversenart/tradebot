from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest

from bot.core.event_bus import EventBus, EventBusClosedError, EventBusFullError
from bot.core.events import AlertLevel, Event, RiskAlert, TickerEvent
from bot.core.models import Ticker


def ticker_event(now: datetime, last: str = "100") -> TickerEvent:
    t = Ticker(exchange="b", symbol="X/Y", timestamp=now, bid=D("99"), ask=D("101"), last=D(last))
    return TickerEvent(ticker=t)


def alert(code: str = "x") -> RiskAlert:
    return RiskAlert(level=AlertLevel.WARNING, code=code, message="m")


async def run_until_drained(bus: EventBus) -> None:
    task = asyncio.create_task(bus.run())
    await bus.stop()
    await asyncio.wait_for(task, timeout=2)


async def test_dispatch_in_order_and_by_type(now: datetime) -> None:
    bus = EventBus()
    tickers: list[str] = []
    alerts: list[str] = []

    async def on_ticker(e: TickerEvent) -> None:
        tickers.append(str(e.ticker.last))

    async def on_alert(e: RiskAlert) -> None:
        alerts.append(e.code)

    bus.subscribe(TickerEvent, on_ticker)
    bus.subscribe(RiskAlert, on_alert)
    for i in range(1, 4):
        await bus.publish(ticker_event(now, str(i)))
    await bus.publish(alert("ip_mismatch"))
    await run_until_drained(bus)

    assert tickers == ["1", "2", "3"]
    assert alerts == ["ip_mismatch"]
    assert bus.dispatched == 4


async def test_base_class_subscription_receives_all(now: datetime) -> None:
    bus = EventBus()
    seen: list[str] = []

    async def on_any(e: Event) -> None:
        seen.append(type(e).__name__)

    bus.subscribe(Event, on_any)
    bus.subscribe(Event, on_any)  # duplicate subscription ignored
    await bus.publish(ticker_event(now))
    await bus.publish(alert())
    await run_until_drained(bus)
    assert seen == ["TickerEvent", "RiskAlert"]


async def test_unsubscribe(now: datetime) -> None:
    bus = EventBus()
    seen: list[Event] = []

    async def h(e: TickerEvent) -> None:
        seen.append(e)

    bus.subscribe(TickerEvent, h)
    bus.unsubscribe(TickerEvent, h)
    bus.unsubscribe(TickerEvent, h)  # no-op
    await bus.publish(ticker_event(now))
    await run_until_drained(bus)
    assert seen == []


async def test_failing_handler_does_not_stop_bus(now: datetime) -> None:
    errors: list[str] = []
    bus = EventBus(on_handler_error=lambda ev, exc: errors.append(f"{type(ev).__name__}:{exc}"))
    ok: list[int] = []

    async def bad(e: TickerEvent) -> None:
        raise RuntimeError("boom")

    async def good(e: TickerEvent) -> None:
        ok.append(1)

    bus.subscribe(TickerEvent, bad)
    bus.subscribe(TickerEvent, good)
    await bus.publish(ticker_event(now))
    await bus.publish(ticker_event(now))
    await run_until_drained(bus)
    assert ok == [1, 1]
    assert bus.handler_errors == 2
    assert errors == ["TickerEvent:boom", "TickerEvent:boom"]


async def test_publish_nowait_full(now: datetime) -> None:
    bus = EventBus(maxsize=1)
    bus.publish_nowait(ticker_event(now))
    with pytest.raises(EventBusFullError):
        bus.publish_nowait(ticker_event(now))
    assert bus.pending == 1


async def test_publish_after_stop_rejected(now: datetime) -> None:
    bus = EventBus()
    await bus.stop()
    await bus.stop()  # idempotent
    with pytest.raises(EventBusClosedError):
        await bus.publish(ticker_event(now))
    with pytest.raises(EventBusClosedError):
        bus.publish_nowait(ticker_event(now))


async def test_live_publish_while_running(now: datetime) -> None:
    bus = EventBus()
    got = asyncio.Event()

    async def h(e: TickerEvent) -> None:
        got.set()

    bus.subscribe(TickerEvent, h)
    task = asyncio.create_task(bus.run())
    await bus.publish(ticker_event(now))
    await asyncio.wait_for(got.wait(), timeout=2)
    with pytest.raises(RuntimeError):
        await bus.run()
    await bus.stop()
    await asyncio.wait_for(task, timeout=2)


def test_maxsize_must_be_positive() -> None:
    with pytest.raises(ValueError):
        EventBus(maxsize=0)


async def test_handlers_may_publish_while_draining(now: datetime) -> None:
    bus = EventBus()
    alerts: list[str] = []

    async def on_ticker(e: TickerEvent) -> None:
        await bus.publish(alert("derived"))

    async def on_alert(e: RiskAlert) -> None:
        alerts.append(e.code)

    bus.subscribe(TickerEvent, on_ticker)
    bus.subscribe(RiskAlert, on_alert)
    for _ in range(3):
        await bus.publish(ticker_event(now))
    await run_until_drained(bus)
    assert alerts == ["derived"] * 3
    with pytest.raises(EventBusClosedError):
        await bus.publish(ticker_event(now))
