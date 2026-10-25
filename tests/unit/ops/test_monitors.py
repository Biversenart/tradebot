from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

from bot.config.schema import LowLiquidityConfig
from bot.core.clock import ManualClock
from bot.core.events import AlertLevel, RiskAlert
from bot.core.models import OrderBook, OrderBookLevel
from bot.ops.balance import ExchangeBalanceCap
from bot.ops.clock_skew import ClockSkewMonitor
from bot.ops.liquidity import LowLiquidityMode
from bot.ops.stablecoin import DepegMonitor
from tests.fakes import FakeAdapter

THU = datetime(2026, 1, 1, 12, tzinfo=UTC)
SAT = datetime(2026, 1, 3, 12, tzinfo=UTC)


class Sink:
    def __init__(self) -> None:
        self.alerts: list[RiskAlert] = []

    async def __call__(self, a: RiskAlert) -> None:
        self.alerts.append(a)

    @property
    def codes(self) -> list[str]:
        return [a.code for a in self.alerts]


def book(amount: str) -> OrderBook:
    return OrderBook(
        exchange="x",
        symbol="BTC/USDT",
        timestamp=THU,
        bids=(OrderBookLevel(price=D("99.9"), amount=D(amount)),),
        asks=(OrderBookLevel(price=D(100), amount=D(amount)),),
    )


# ------------------------------------------------------------------ low liquidity
def test_weekend_and_holiday_factor() -> None:
    m = LowLiquidityMode(
        LowLiquidityConfig(holidays=(date(2026, 1, 1),), holiday_risk_factor=D("0.3"))
    )
    assert m.assess(datetime(2026, 1, 2, tzinfo=UTC)) == (D(1), [])
    f, notes = m.assess(SAT)
    assert f == D("0.5") and notes == ["hafta sonu"]
    f, notes = m.assess(THU)
    assert f == D("0.3") and notes == ["tatil günü"]


def test_thin_book_and_halt_action() -> None:
    cfg = LowLiquidityConfig(min_depth_quote=D(5000), action="halt")
    m = LowLiquidityMode(cfg)
    assert m.assess(THU, book("100"))[0] == D(1)  # ~10k each side
    f, notes = m.assess(THU, book("10"))
    assert f == 0 and "ince orderbook" in notes[0]
    assert LowLiquidityMode(LowLiquidityConfig(enabled=False)).assess(SAT) == (D(1), [])


# ------------------------------------------------------------------ depeg
async def test_depeg_blocks_and_recovers_with_hysteresis() -> None:
    sink = Sink()
    m = DepegMonitor(D("0.5"), ("USDC/USDT",), sink)
    await m.update({"USDC/USDT": D("0.998")})
    assert m.block_reason() is None
    await m.update({"USDC/USDT": D("0.990")})
    reason = m.block_reason()
    assert reason is not None and "USDC/USDT" in reason
    assert sink.alerts[-1].level is AlertLevel.CRITICAL
    await m.update({"USDC/USDT": D("0.996")})  # 0.4 % > half threshold: still blocked
    assert m.block_reason() is not None
    await m.update({"USDC/USDT": D("0.999")})
    assert m.block_reason() is None and sink.codes[-1] == "stablecoin_depeg_recovered"


async def test_depeg_missing_price_reported_once_never_blocks() -> None:
    sink = Sink()
    m = DepegMonitor(D("0.5"), ("FDUSD/USDT",), sink)
    await m.update({})
    await m.update({})
    assert sink.codes == ["stablecoin_price_missing"] and m.block_reason() is None


# ------------------------------------------------------------------ clock skew
async def test_clock_skew_blocks_exchange() -> None:
    sink = Sink()
    ad = FakeAdapter("binance")
    m = ClockSkewMonitor(1000, sink)
    ad.time_offset = timedelta(milliseconds=200)
    await m.check({"binance": ad})
    assert m.block_reason("binance") is None
    ad.time_offset = timedelta(seconds=-3)
    await m.check({"binance": ad})
    assert "saat kayması" in (m.block_reason("binance") or "")
    assert m.block_reason("other") is None
    ad.time_offset = timedelta(milliseconds=100)
    await m.check({"binance": ad})
    assert m.block_reason("binance") is None
    assert sink.codes == ["clock_skew", "clock_skew_recovered"]


# ------------------------------------------------------------------ balance cap
async def test_balance_cap_alerts_with_repeat_interval() -> None:
    sink = Sink()
    clock = ManualClock(THU)
    cap = ExchangeBalanceCap(D(5000), 6, sink, clock)
    assert await cap.check({"binance": D(4000)}) == []
    assert await cap.check({"binance": D(6000), "btcturk": D(10)}) == ["binance"]
    await cap.check({"binance": D(6000)})
    assert sink.codes == ["exchange_balance_cap"]  # not repeated within 6 h
    clock.advance(timedelta(hours=7))
    await cap.check({"binance": D(6000)})
    assert sink.codes == ["exchange_balance_cap"] * 2
