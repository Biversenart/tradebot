"""Build candles from the public trade tape (1m … 1d, 1w).

Intervals without trades produce flat candles (open=high=low=close=previous close,
volume 0), matching exchange kline behaviour, so series stay gap-free.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bot.core.models import Candle, Trade
from bot.core.timeframes import floor_time, timeframe_delta


@dataclass
class _Building:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class CandleBuilder:
    """Aggregates trades for one (exchange, symbol, timeframe)."""

    def __init__(self, exchange: str, symbol: str, timeframe: str) -> None:
        self.exchange = exchange
        self.symbol = symbol
        self.timeframe = timeframe
        self._delta = timeframe_delta(timeframe)
        self._cur: _Building | None = None
        self._last_close: Decimal | None = None

    def _emit(self, b: _Building, closed: bool) -> Candle:
        return Candle(
            exchange=self.exchange,
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time=b.open_time,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
            closed=closed,
        )

    def _roll_until(self, start: datetime) -> list[Candle]:
        """Close the current candle and emit flat candles up to (excluding) `start`."""
        out: list[Candle] = []
        if self._cur is None:
            return out
        out.append(self._emit(self._cur, closed=True))
        self._last_close = self._cur.close
        t = self._cur.open_time + self._delta
        while t < start:
            p = self._last_close
            out.append(self._emit(_Building(t, p, p, p, p, Decimal(0)), closed=True))
            t += self._delta
        self._cur = None
        return out

    def add_trade(self, trade: Trade) -> list[Candle]:
        """Add a trade; returns candles that closed because of it."""
        start = floor_time(trade.timestamp, self.timeframe)
        closed: list[Candle] = []
        if self._cur is not None and start < self._cur.open_time:
            return closed  # late trade for an already-closed candle: ignored
        if self._cur is not None and start > self._cur.open_time:
            closed = self._roll_until(start)
        if self._cur is None:
            p = trade.price
            self._cur = _Building(start, p, p, p, p, Decimal(0))
        c = self._cur
        c.high = max(c.high, trade.price)
        c.low = min(c.low, trade.price)
        c.close = trade.price
        c.volume += trade.amount
        return closed

    def flush(self, now: datetime) -> list[Candle]:
        """Close candles whose interval ended before `now` (call periodically)."""
        if self._cur is None or now < self._cur.open_time + self._delta:
            return []
        current_start = floor_time(now, self.timeframe)
        closed = self._roll_until(current_start)
        p = self._last_close
        if p is not None:
            self._cur = _Building(current_start, p, p, p, p, Decimal(0))
        return closed

    @property
    def current(self) -> Candle | None:
        return self._emit(self._cur, closed=False) if self._cur else None


class MultiTimeframeBuilder:
    def __init__(self, exchange: str, symbol: str, timeframes: Iterable[str]) -> None:
        self.builders = {tf: CandleBuilder(exchange, symbol, tf) for tf in timeframes}

    def add_trade(self, trade: Trade) -> list[Candle]:
        out: list[Candle] = []
        for b in self.builders.values():
            out.extend(b.add_trade(trade))
        return out

    def flush(self, now: datetime) -> list[Candle]:
        out: list[Candle] = []
        for b in self.builders.values():
            out.extend(b.flush(now))
        return out
