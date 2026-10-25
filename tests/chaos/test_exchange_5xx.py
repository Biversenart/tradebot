"""Chaos: exchange 5xx / maintenance responses."""

from __future__ import annotations

import ccxt
import pytest

from bot.core.events import SignalEvent
from bot.exchanges.ccxt_adapter import translate_error
from bot.exchanges.errors import ExchangeNetworkError, ExchangeUnavailableError
from bot.net.errors import ErrorKind
from bot.risk.kill_switch import KillReason
from tests.chaos.conftest import ChaosWorld
from tests.unit.execution.test_coordinator import signal


def e503() -> ExchangeUnavailableError:
    return ExchangeUnavailableError("503 Service Unavailable")


@pytest.mark.parametrize(
    ("exc", "cls"),
    [
        (ccxt.ExchangeNotAvailable("binance 503 Service Unavailable"), ExchangeUnavailableError),
        (ccxt.OnMaintenance("binance system maintenance"), ExchangeUnavailableError),
        (ccxt.RequestTimeout("timed out"), ExchangeNetworkError),
    ],
)
def test_5xx_translated_to_retryable_errors(exc: Exception, cls: type) -> None:
    err = translate_error(exc)
    assert isinstance(err, cls) and err.kind.counts_for_kill_switch


async def test_transient_5xx_retried_idempotently(chaos: ChaosWorld) -> None:
    chaos.ex.inject("create_order", e503(), e503())
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    [pos] = await chaos.repo.open_positions()
    assert pos.stop_order_id is not None  # protected despite the outage
    assert len(chaos.paper._orders) == 2  # one entry + one stop: retries did not duplicate
    assert chaos.ks.state.consecutive_errors == 0  # reset by the success
    assert not chaos.ks.is_active


async def test_persistent_5xx_trips_kill_switch_and_blocks_entries(chaos: ChaosWorld) -> None:
    chaos.ex.inject("create_order", *[e503() for _ in range(4)])
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    assert await chaos.repo.open_positions() == []
    assert KillReason.ERRORS in chaos.ks.active_reasons  # 3 consecutive (config)
    await chaos.coord.on_signal(SignalEvent(signal=signal()))  # exchange back, but halted
    assert await chaos.repo.open_positions() == []
    assert chaos.ex.calls["create_order"] == 4


async def test_5xx_on_stop_placement_closes_position(chaos: ChaosWorld) -> None:
    # entry passes, the stop fails on every retry -> rule 9: close immediately
    chaos.ex.inject("create_order", None, *[e503() for _ in range(4)])
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    assert await chaos.repo.open_positions() == []
    [closed] = await chaos.repo.closed_positions(5)
    assert closed.stop_order_id is None and "stop" in (closed.close_reason or "")
    base = await chaos.paper.fetch_balance()
    assert base.get("BTC") is None or base["BTC"].total == 0  # nothing left unprotected


async def test_5xx_in_maintenance_loop_does_not_crash(chaos: ChaosWorld) -> None:
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    chaos.ex.inject("fetch_balance", e503())
    chaos.ex.inject("fetch_order_by_client_id", e503())
    chaos.ex.inject("fetch_order", e503())
    await chaos.coord.tick()  # errors are logged, nothing raises
    await chaos.coord.tick()
    assert len(await chaos.repo.open_positions()) == 1
    assert ErrorKind.SERVER.counts_for_kill_switch
