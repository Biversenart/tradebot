"""Load OHLCV frames for analysis: Parquet first, resample from a lower timeframe, else fetch."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pandas as pd

from bot.core.timeframes import timeframe_seconds
from bot.exchanges.base import ExchangeAdapter
from bot.marketdata.history import candles_to_frame, load_ohlcv, parquet_path, warm_up

_WEEK_ANCHOR_NS = pd.Timestamp("1970-01-05", tz="UTC").value  # Monday


def resample_ohlcv(df: pd.DataFrame, timeframe: str, drop_incomplete: bool = True) -> pd.DataFrame:
    """Aggregate to a higher timeframe (UTC buckets; weeks start Monday like Binance)."""
    if df.empty:
        return df
    step = timeframe_seconds(timeframe) * 1_000_000_000
    idx = pd.DatetimeIndex(df.index)
    naive = idx.tz_convert("UTC").tz_localize(None) if idx.tz is not None else idx
    ns = naive.to_numpy(dtype="datetime64[ns]").astype(np.int64)
    anchor = _WEEK_ANCHOR_NS if timeframe.endswith("w") else 0
    buckets = anchor + (ns - anchor) // step * step
    key = pd.DatetimeIndex(pd.to_datetime(buckets, utc=True), name="open_time")
    g = df.groupby(key)
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }
    )
    if drop_incomplete and len(idx) >= 2:
        base = int(np.median(np.diff(ns)))
        expected = step // base if base > 0 else 1
        counts = g.size()
        if counts.iloc[-1] < expected:
            out = out.iloc[:-1]
    return out


def _lower_sources(root: Path, exchange: str, symbol: str, tf: str) -> Iterable[tuple[str, Path]]:
    target = timeframe_seconds(tf)
    cands = ["1m", "5m", "15m", "1h", "4h", "1d"]
    for src in sorted(cands, key=timeframe_seconds, reverse=True):
        s = timeframe_seconds(src)
        if s < target and target % s == 0:
            path = parquet_path(root, exchange, symbol, src)
            if path.exists():
                yield src, path


async def load_frames(
    symbol: str,
    timeframes: Iterable[str],
    *,
    exchange: str,
    data_root: Path,
    bars: int,
    adapter: ExchangeAdapter | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Returns (frames, source per timeframe: 'parquet' | 'resample:<tf>' | 'exchange')."""
    frames: dict[str, pd.DataFrame] = {}
    sources: dict[str, str] = {}
    for tf in timeframes:
        path = parquet_path(data_root, exchange, symbol, tf)
        if path.exists():
            frames[tf] = load_ohlcv(path).iloc[-bars:]
            sources[tf] = "parquet"
            continue
        src = next(iter(_lower_sources(data_root, exchange, symbol, tf)), None)
        if src is not None:
            frames[tf] = resample_ohlcv(load_ohlcv(src[1]), tf).iloc[-bars:]
            sources[tf] = f"resample:{src[0]}"
            continue
        if adapter is not None:
            candles = await warm_up(adapter, symbol, tf, bars)
            if candles:
                frames[tf] = candles_to_frame(candles)
                sources[tf] = "exchange"
    return frames, sources
