"""Historical OHLCV: paginated download, gap detection/filling, Parquet storage.

Parquet stores floats (ccxt delivers OHLCV as floats anyway); conversion to `Decimal`
happens via `str()` when Candle models are built. Indicator maths may use floats; money
maths (orders, PnL) always uses Decimal.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pandas as pd

from bot.core.models import Candle
from bot.core.timeframes import floor_time, timeframe_delta
from bot.exchanges.base import ExchangeAdapter
from bot.log import get_logger

_log = get_logger(__name__)
COLUMNS = ["open", "high", "low", "close", "volume"]


def find_gaps(candles: Sequence[Candle], timeframe: str) -> list[tuple[datetime, datetime]]:
    """Missing intervals as (first_missing_open_time, last_missing_open_time)."""
    step = timeframe_delta(timeframe)
    gaps: list[tuple[datetime, datetime]] = []
    for prev, cur in pairwise(candles):
        expected = prev.open_time + step
        if cur.open_time > expected:
            gaps.append((expected, cur.open_time - step))
    return gaps


def merge_candles(*series: Sequence[Candle]) -> list[Candle]:
    """Merge, de-duplicate (last wins) and sort by open_time."""
    by_time: dict[datetime, Candle] = {}
    for s in series:
        for c in s:
            by_time[c.open_time] = c
    return [by_time[t] for t in sorted(by_time)]


async def fetch_history(
    adapter: ExchangeAdapter,
    symbol: str,
    timeframe: str,
    since: datetime,
    until: datetime | None = None,
    batch: int = 1000,
    max_batches: int = 100_000,
) -> list[Candle]:
    """Download closed candles in [since, until) by paginating `fetch_ohlcv`."""
    step = timeframe_delta(timeframe)
    until = until or datetime.now(UTC)
    cursor = floor_time(since, timeframe)
    out: list[Candle] = []
    for _ in range(max_batches):
        if cursor >= until:
            break
        rows = await adapter.fetch_ohlcv(symbol, timeframe, since=cursor, limit=batch)
        rows = [c for c in rows if c.open_time >= cursor and c.open_time < until and c.closed]
        if not rows:
            break
        out.extend(rows)
        nxt = rows[-1].open_time + step
        if nxt <= cursor:
            break
        cursor = nxt
    return merge_candles(out)


async def fill_gaps(
    adapter: ExchangeAdapter, candles: Sequence[Candle], timeframe: str
) -> list[Candle]:
    if not candles:
        return []
    symbol = candles[0].symbol
    patches: list[Candle] = []
    step = timeframe_delta(timeframe)
    for start, end in find_gaps(candles, timeframe):
        patches.extend(await fetch_history(adapter, symbol, timeframe, start, end + step))
    return merge_candles(candles, patches)


async def warm_up(
    adapter: ExchangeAdapter, symbol: str, timeframe: str, bars: int, now: datetime | None = None
) -> list[Candle]:
    """Fetch the last `bars` closed candles (strategy/indicator warm-up)."""
    now = now or datetime.now(UTC)
    since = floor_time(now, timeframe) - timeframe_delta(timeframe) * bars
    candles = await fetch_history(adapter, symbol, timeframe, since, until=now)
    return (await fill_gaps(adapter, candles, timeframe))[-bars:]


# ---------------------------------------------------------------- Parquet storage


def safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "-").replace(":", "_")


def parquet_path(root: Path, exchange: str, symbol: str, timeframe: str) -> Path:
    return root / "ohlcv" / exchange / safe_symbol(symbol) / f"{timeframe}.parquet"


def candles_to_frame(candles: Sequence[Candle]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "open_time": [c.open_time for c in candles],
            **{col: [float(getattr(c, col)) for c in candles] for col in COLUMNS},
        }
    )
    if df.empty:
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    return df.set_index("open_time").sort_index()


def frame_to_candles(df: pd.DataFrame, exchange: str, symbol: str, timeframe: str) -> list[Candle]:
    out: list[Candle] = []
    index = pd.DatetimeIndex(df.index)
    cols = [df[c].tolist() for c in COLUMNS]
    for i, ts in enumerate(index):
        o, h, low, c, v = (Decimal(str(col[i])) for col in cols)
        out.append(
            Candle(
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                open_time=ts.to_pydatetime(),
                open=o,
                high=h,
                low=low,
                close=c,
                volume=v,
            )
        )
    return out


def load_ohlcv(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def save_ohlcv(path: Path, df: pd.DataFrame) -> pd.DataFrame:
    """Merge with an existing file (new rows win), then write atomically."""
    if path.exists():
        old = load_ohlcv(path)
        df = pd.concat([old, df])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp)
    tmp.replace(path)
    return df


async def download_to_parquet(
    adapter: ExchangeAdapter,
    symbol: str,
    timeframe: str,
    since: datetime,
    root: Path,
    until: datetime | None = None,
) -> tuple[Path, int]:
    """Incremental download: resumes after the last stored candle."""
    path = parquet_path(root, adapter.name, symbol, timeframe)
    start = since
    if path.exists():
        existing = load_ohlcv(path)
        if not existing.empty:
            last = pd.Timestamp(existing.index[-1]).to_pydatetime()
            start = max(since, last + timeframe_delta(timeframe))
    candles = await fetch_history(adapter, symbol, timeframe, start, until)
    candles = await fill_gaps(adapter, candles, timeframe)
    df = save_ohlcv(path, candles_to_frame(candles))
    _log.info(
        "ohlcv_downloaded", symbol=symbol, timeframe=timeframe, new=len(candles), total=len(df)
    )
    gaps = find_gaps(frame_to_candles(df, adapter.name, symbol, timeframe), timeframe)
    if gaps:
        _log.warning("ohlcv_gaps_remaining", count=len(gaps), first=str(gaps[0][0]))
    return path, len(candles)


def utc_date(value: str) -> datetime:
    """Parse YYYY-MM-DD (or ISO datetime) as UTC."""
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


__all__ = [
    "candles_to_frame",
    "download_to_parquet",
    "fetch_history",
    "fill_gaps",
    "find_gaps",
    "frame_to_candles",
    "load_ohlcv",
    "merge_candles",
    "parquet_path",
    "save_ohlcv",
    "utc_date",
    "warm_up",
]
