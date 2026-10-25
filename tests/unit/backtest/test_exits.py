from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

from bot.core.models import PositionSide
from bot.portfolio.exits import ExitFill, ExitRules, ManagedPosition, on_bar

T = datetime(2024, 1, 1, tzinfo=UTC)
RULES = ExitRules(tp1_close_pct=D(40), trailing="atr", trailing_atr_mult=D(2), time_exit_bars=10)


def long_pos() -> ManagedPosition:
    return ManagedPosition(
        "X", "s", PositionSide.LONG, D(100), D(10), D(95), D(95), [D(105), D(110), D(115)], T, 0
    )


def short_pos() -> ManagedPosition:
    return ManagedPosition(
        "X", "s", PositionSide.SHORT, D(100), D(10), D(105), D(105), [D(95), D(90), D(85)], T, 0
    )


def bar(
    p: ManagedPosition,
    i: int,
    o: str,
    h: str,
    lo: str,
    c: str,
    atr: str | None = "1",
    rules: ExitRules = RULES,
) -> list[ExitFill]:
    return on_bar(p, rules, i, D(o), D(h), D(lo), D(c), D(atr) if atr else None, None)


def test_stop_hit_before_targets_same_bar() -> None:
    p = long_pos()
    fills = bar(p, 1, "100", "106", "94", "100")  # touches TP1 and stop: stop wins
    assert [(f.reason, f.price, f.qty) for f in fills] == [("stop", D(95), D(10))]
    assert not p.is_open


def test_gap_through_stop_exits_at_open() -> None:
    p = long_pos()
    fills = bar(p, 1, "93", "94", "90", "92")
    assert fills[0].reason == "stop_gap" and fills[0].price == D(93)


def test_tp1_partial_breakeven_then_trailing() -> None:
    p = long_pos()
    fills = bar(p, 1, "101", "105.5", "100.5", "105")
    assert [(f.reason, f.qty) for f in fills] == [("tp1", D(4))]
    # BE (100) then trailing from the close: 105 - 2*ATR(1) = 103 (checked from the next bar)
    assert p.qty == D(6) and p.stop == D(103) and p.stop_reason == "trailing_stop"
    bar(p, 2, "105", "107", "104.5", "107")
    assert p.stop == D(105) and p.stop_reason == "trailing_stop"
    bar(p, 3, "106", "106.5", "105.5", "106")  # lower close: stop never loosens
    assert p.stop == D(105)
    fills = bar(p, 4, "105.5", "105.6", "104", "104.5")
    assert fills[0].reason == "trailing_stop" and fills[0].price == D(105) and not p.is_open


def test_all_targets() -> None:
    p = long_pos()
    fills = bar(p, 1, "101", "116", "100.5", "115")
    assert [(f.reason, f.qty) for f in fills] == [("tp1", D(4)), ("tp2", D(3)), ("tp3", D(3))]
    assert not p.is_open


def test_time_exit_only_without_targets() -> None:
    p = long_pos()
    for i in range(1, 10):
        assert bar(p, i, "100", "101", "99", "100") == []
    fills = bar(p, 10, "100", "101", "99", "100.5")
    assert fills[0].reason == "time" and fills[0].price == D("100.5")


def test_short_mirror() -> None:
    p = short_pos()
    fills = bar(p, 1, "99", "99.5", "94.5", "95")
    assert fills[0].reason == "tp1" and p.stop == D(97)  # 95 + 2*ATR
    fills = bar(p, 2, "96", "106", "95.5", "100")
    assert fills[0].reason == "trailing_stop" and fills[0].price == D(97)
    assert p.unrealized(D(90)) == 0  # closed


def test_no_trailing_rule() -> None:
    rules = ExitRules(trailing="none", time_exit_bars=100)
    p = long_pos()
    bar(p, 1, "101", "105.5", "100.5", "105", rules=rules)
    assert p.stop == D(100)  # break-even only
    bar(p, 2, "105", "109", "104.5", "109", rules=rules)
    assert p.stop == D(100)
