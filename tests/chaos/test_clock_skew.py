"""Chaos: local clock drifts away from the exchange clock."""

from __future__ import annotations

from datetime import timedelta

import ccxt

from bot.config import Mode
from bot.core.events import SignalEvent
from bot.exchanges.ccxt_adapter import CcxtAdapter, translate_error
from bot.exchanges.errors import ExchangeAdapterError, ExchangeUnavailableError
from bot.ops.service import OpsService
from tests.chaos.conftest import ChaosWorld
from tests.unit.execution.test_coordinator import signal


def ops_for(chaos: ChaosWorld) -> OpsService:
    svc = OpsService(
        chaos.cfg,
        Mode.PAPER,
        {"paper": chaos.ex},
        chaos.coord.on_alert,
        clock=chaos.clock,
        sources=[],
    )
    chaos.risk.ops = svc.guard
    chaos.coord.ops = svc
    return svc


async def test_skew_blocks_new_orders_until_clock_is_fixed(chaos: ChaosWorld) -> None:
    svc = ops_for(chaos)
    chaos.ex.time_offset = timedelta(seconds=-4)  # local clock 4 s ahead of the exchange
    await svc.check()
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    assert await chaos.repo.open_positions() == []
    reports = await chaos.repo.recent_risk_events(5)
    assert any(e.code == "clock_skew" for e in reports)
    chaos.ex.time_offset = timedelta(milliseconds=300)
    await svc.check()
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    assert len(await chaos.repo.open_positions()) == 1
    codes = [e.code for e in await chaos.repo.recent_risk_events(10)]
    assert "clock_skew_recovered" in codes


async def test_time_endpoint_failure_does_not_block_or_crash(chaos: ChaosWorld) -> None:
    svc = ops_for(chaos)
    chaos.ex.inject("server_time_offset", ExchangeUnavailableError("503"))
    await svc.check()
    assert svc.guard.block_reason("paper", "BTC/USDT") is None
    await chaos.coord.on_signal(SignalEvent(signal=signal()))
    assert len(await chaos.repo.open_positions()) == 1


def test_ccxt_compensates_and_timestamp_errors_are_typed() -> None:
    client = CcxtAdapter.build_client("binance", proxy_options={}, pro=False)
    assert client.options["adjustForTimeDifference"] is True
    err = translate_error(
        ccxt.InvalidNonce('binance {"code":-1021,"msg":"Timestamp outside of the recvWindow."}')
    )
    assert isinstance(err, ExchangeAdapterError) and "-1021" in str(err)
