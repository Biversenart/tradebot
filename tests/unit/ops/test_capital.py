from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from bot.config.schema import Mode, OperationsConfig
from bot.core.clock import ManualClock
from bot.ops.capital import CapitalCap, CapitalCapError

T = datetime(2026, 1, 1, tzinfo=UTC)


def cap(mode: Mode = Mode.LIVE, **kw: object) -> tuple[CapitalCap, ManualClock]:
    clock = ManualClock(T)
    c = CapitalCap(OperationsConfig.model_validate(kw), mode, clock)
    c.restore(None)
    return c, clock


def test_live_equity_capped_paper_not() -> None:
    c, _ = cap()
    assert c.effective_equity(D(10_000)) == D(1000)  # default 10 %
    p, _ = cap(Mode.PAPER)
    assert p.effective_equity(D(10_000)) == D(10_000)


def test_raise_needs_canary_period_and_is_gradual() -> None:
    c, clock = cap()
    with pytest.raises(CapitalCapError, match="Kanarya"):
        c.set_cap(D(20), "ali")
    clock.advance(timedelta(days=14))
    with pytest.raises(CapitalCapError, match="Kademeli"):
        c.set_cap(D(50), "ali")
    assert "%20" in c.set_cap(D(20), "ali")
    assert c.effective_equity(D(10_000)) == D(2000)
    # next raise waits another canary period
    with pytest.raises(CapitalCapError):
        c.set_cap(D(40), "ali")
    assert c.state.history[-1]["by"] == "ali"


def test_lowering_always_allowed_and_bounds() -> None:
    c, _ = cap()
    c.set_cap(D(5), "x")
    assert c.cap_pct == D(5)
    with pytest.raises(CapitalCapError):
        c.set_cap(D(0), "x")
    with pytest.raises(CapitalCapError):
        c.set_cap(D(101), "x")


def test_persisted_state_and_config_precedence() -> None:
    c, clock = cap()
    clock.advance(timedelta(days=15))
    c.set_cap(D(20), "x")
    data = c.state.to_dict()
    # higher config value is ignored (raising must be approved)
    hi = CapitalCap(OperationsConfig(live_capital_cap_pct=D(50)), Mode.LIVE, clock)
    hi.restore(data)
    assert hi.cap_pct == D(20)
    # lower config value wins (safer)
    lo = CapitalCap(OperationsConfig(live_capital_cap_pct=D(5)), Mode.LIVE, clock)
    lo.restore(data)
    assert lo.cap_pct == D(5)
    assert hi.state.started_at == T
