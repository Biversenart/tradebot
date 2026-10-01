from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.analysis.data import load_frames, resample_ohlcv
from bot.marketdata.history import parquet_path
from tests.fakes import FakeAdapter, make_candles
from tests.fixtures.loader import synthetic_1h

DF = synthetic_1h()


def test_resample_4h_aggregation() -> None:
    r = resample_ohlcv(DF, "4h")
    first = DF.iloc[:4]
    row = r.iloc[0]
    assert r.index[0] == DF.index[0]
    assert row["open"] == first["open"].iloc[0]
    assert row["high"] == first["high"].max()
    assert row["low"] == first["low"].min()
    assert row["close"] == first["close"].iloc[-1]
    assert row["volume"] == pytest.approx(first["volume"].sum())
    assert len(r) == len(DF) // 4


def test_resample_weekly_monday_and_drops_incomplete() -> None:
    w = resample_ohlcv(DF, "1w")
    assert all(ts.weekday() == 0 for ts in w.index)
    assert len(w) == len(DF) // (24 * 7)
    partial = resample_ohlcv(DF.iloc[:30], "1d")
    assert len(partial) == 1  # 24h complete + 6h partial dropped
    assert len(resample_ohlcv(DF.iloc[:30], "1d", drop_incomplete=False)) == 2


async def test_load_frames_sources(tmp_path: Path) -> None:
    p = parquet_path(tmp_path, "binance", "BTC/USDT", "1h")
    p.parent.mkdir(parents=True)
    DF.to_parquet(p)
    frames, sources = await load_frames(
        "BTC/USDT", ["1h", "4h", "15m"], exchange="binance", data_root=tmp_path, bars=100
    )
    assert sources == {"1h": "parquet", "4h": "resample:1h"}
    assert len(frames["1h"]) == 100 and "15m" not in frames


async def test_load_frames_falls_back_to_exchange(tmp_path: Path) -> None:
    start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=15 * 400)
    start = start.replace(minute=start.minute - start.minute % 15)
    ad = FakeAdapter(history=make_candles(400, start=start, timeframe="15m"))
    frames, sources = await load_frames(
        "BTC/USDT", ["15m"], exchange="binance", data_root=tmp_path, bars=50, adapter=ad
    )
    assert sources == {"15m": "exchange"}
    assert len(frames["15m"]) <= 50
