"""Every kill switch trigger (CLAUDE.md rule 5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from bot.config.schema import RiskConfig
from bot.core.clock import ManualClock
from bot.core.events import RiskAlert
from bot.net.errors import ErrorKind
from bot.risk.kill_switch import KillReason, KillSwitch, KillSwitchState

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def make(**risk: object) -> tuple[KillSwitch, ManualClock, list[RiskAlert]]:
    clock = ManualClock(T0)
    alerts: list[RiskAlert] = []

    async def sink(a: RiskAlert) -> None:
        alerts.append(a)

    return KillSwitch(RiskConfig.model_validate(risk), clock, sink), clock, alerts


async def test_daily_loss_pauses_until_next_utc_day_without_closing() -> None:
    ks, clock, alerts = make(daily_loss_limit_pct=3)
    await ks.record_equity(D(10_000))
    await ks.record_equity(D(9_750))
    assert ks.allows_new_orders()
    await ks.record_equity(D(9_690))  # -3.1%
    assert not ks.allows_new_orders()
    assert ks.active_reasons == [KillReason.DAILY_LOSS]
    assert not ks.close_positions_requested
    assert alerts[-1].code == "kill_switch_daily_loss"
    assert await ks.resume() == []  # manual resume cannot lift the daily pause
    clock.set(datetime(2026, 3, 3, 0, 0, tzinfo=UTC))
    assert ks.allows_new_orders()
    await ks.record_equity(D(9_690))  # new day: new baseline
    assert ks.allows_new_orders()


async def test_max_drawdown_closes_positions_and_needs_manual_resume() -> None:
    ks, clock, alerts = make(max_drawdown_pct=10, daily_loss_limit_pct=50)
    await ks.record_equity(D(10_000))
    await ks.record_equity(D(12_000))
    clock.advance(timedelta(days=3))
    await ks.record_equity(D(10_700))  # 10.8% below the 12k peak
    assert not ks.allows_new_orders()
    assert KillReason.MAX_DRAWDOWN in ks.active_reasons
    assert ks.close_positions_requested
    assert "kapatılacak" in alerts[-1].message
    assert await ks.resume("panel") == ["max_drawdown"]
    assert ks.allows_new_orders() and not ks.close_positions_requested
    await ks.record_equity(D(10_700))  # drawdown is re-measured from here
    assert ks.allows_new_orders()


async def test_keep_positions_config() -> None:
    ks, _, _ = make(max_drawdown_pct=10, on_kill_switch="keep_positions")
    await ks.record_equity(D(100))
    await ks.record_equity(D(80))
    assert not ks.allows_new_orders() and not ks.close_positions_requested


async def test_consecutive_errors() -> None:
    ks, _, alerts = make(max_consecutive_errors=3)
    await ks.record_error(ErrorKind.RATE_LIMIT)  # does not count
    await ks.record_error(ErrorKind.NETWORK)
    await ks.record_error(ErrorKind.SERVER)
    ks.record_success()  # resets
    await ks.record_error(ErrorKind.NETWORK)
    await ks.record_error(ErrorKind.INVALID_IP)
    assert ks.allows_new_orders()
    await ks.record_error(ErrorKind.RESTRICTED_LOCATION, "451")
    assert not ks.allows_new_orders()
    assert ks.active_reasons == [KillReason.ERRORS]
    assert "451" in alerts[-1].message
    await ks.resume()
    assert ks.allows_new_orders()


async def test_egress_ip_block_clears_automatically() -> None:
    ks, _, alerts = make()
    await ks.set_egress_ok(False, "(mismatch)")
    assert not ks.allows_new_orders()
    assert await ks.resume() == []  # cannot be lifted manually
    assert not ks.allows_new_orders()
    await ks.set_egress_ok(True)
    assert ks.allows_new_orders()
    assert alerts[-1].code == "kill_switch_egress_cleared"


async def test_manual_trigger_and_alert_once() -> None:
    ks, _, alerts = make()
    await ks.trigger(KillReason.MANUAL, "Panelden durduruldu.")
    await ks.trigger(KillReason.MANUAL, "Panelden durduruldu.")
    assert len([a for a in alerts if a.code == "kill_switch_manual"]) == 1
    assert ks.close_positions_requested


async def test_state_persists_across_restart() -> None:
    ks, clock, _ = make(max_drawdown_pct=10)
    await ks.record_equity(D(100))
    await ks.record_equity(D(85))
    data = ks.state.to_dict()
    restored = KillSwitch(ks.risk, clock, state=KillSwitchState.from_dict(data))
    assert not restored.allows_new_orders()
    assert restored.state.peak_equity == D(100)
