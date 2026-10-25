from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from bot.core.models import Side, Trade
from bot.marketdata.candles import CandleBuilder, MultiTimeframeBuilder

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def tr(sec: float, price: str, amount: str = "1", tid: str = "") -> Trade:
    return Trade(
        exchange="x",
        symbol="BTC/USDT",
        trade_id=tid or str(sec),
        price=D(price),
        amount=D(amount),
        side=Side.BUY,
        timestamp=T0 + timedelta(seconds=sec),
    )


def test_aggregates_ohlcv() -> None:
    b = CandleBuilder("x", "BTC/USDT", "1m")
    for sec, p in [(1, "100"), (10, "105"), (20, "98"), (59, "101")]:
        assert b.add_trade(tr(sec, p)) == []
    closed = b.add_trade(tr(61, "102"))
    assert len(closed) == 1
    c = closed[0]
    assert (c.open, c.high, c.low, c.close, c.volume) == (D(100), D(105), D(98), D(101), D(4))
    assert c.open_time == T0 and c.closed
    assert b.current is not None and b.current.open == D(102) and not b.current.closed


def test_gap_produces_flat_candles() -> None:
    b = CandleBuilder("x", "BTC/USDT", "1m")
    b.add_trade(tr(5, "100"))
    closed = b.add_trade(tr(3 * 60 + 5, "110"))
    assert [c.open_time for c in closed] == [T0 + timedelta(minutes=i) for i in range(3)]
    flat = closed[1]
    assert flat.open == flat.high == flat.low == flat.close == D(100)
    assert flat.volume == 0


def test_late_trade_ignored() -> None:
    b = CandleBuilder("x", "BTC/USDT", "1m")
    b.add_trade(tr(65, "100"))
    assert b.add_trade(tr(30, "1")) == []
    assert b.current is not None and b.current.low == D(100)


def test_flush_closes_on_time() -> None:
    b = CandleBuilder("x", "BTC/USDT", "1m")
    b.add_trade(tr(5, "100"))
    assert b.flush(T0 + timedelta(seconds=50)) == []
    closed = b.flush(T0 + timedelta(minutes=2, seconds=1))
    assert [c.open_time for c in closed] == [T0, T0 + timedelta(minutes=1)]
    cur = b.current
    assert cur is not None and cur.open_time == T0 + timedelta(minutes=2) and cur.volume == 0


def test_multi_timeframe() -> None:
    m = MultiTimeframeBuilder("x", "BTC/USDT", ["1m", "5m"])
    closed = []
    for i in range(6 * 60 + 1):
        closed += m.add_trade(tr(i, str(100 + i % 7)))
    assert sum(c.timeframe == "1m" for c in closed) == 6
    five = [c for c in closed if c.timeframe == "5m"]
    assert len(five) == 1 and five[0].volume == D(300)
    assert five[0].high == D(106) and five[0].low == D(100)
