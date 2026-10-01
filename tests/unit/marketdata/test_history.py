from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from bot.marketdata.history import (
    candles_to_frame,
    download_to_parquet,
    fetch_history,
    fill_gaps,
    find_gaps,
    frame_to_candles,
    load_ohlcv,
    merge_candles,
    parquet_path,
    utc_date,
    warm_up,
)
from tests.fakes import FakeAdapter, make_candles

T0 = datetime(2024, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)


def test_find_gaps() -> None:
    c = make_candles(10)
    holed = c[:3] + c[6:]
    assert find_gaps(holed, "1h") == [(T0 + 3 * H, T0 + 5 * H)]
    assert find_gaps(c, "1h") == []


def test_merge_dedup_sorted() -> None:
    c = make_candles(5)
    merged = merge_candles(c[3:], c[:4])
    assert [x.open_time for x in merged] == [x.open_time for x in c]


async def test_fetch_history_paginates() -> None:
    ad = FakeAdapter(history=make_candles(2500))
    out = await fetch_history(ad, "BTC/USDT", "1h", T0, until=T0 + 2500 * H, batch=1000)
    assert len(out) == 2500
    assert len(ad.ohlcv_calls) == 3
    assert ad.ohlcv_calls[1][0] == T0 + 1000 * H


async def test_fetch_history_respects_until() -> None:
    ad = FakeAdapter(history=make_candles(100))
    out = await fetch_history(ad, "BTC/USDT", "1h", T0 + 10 * H, until=T0 + 20 * H)
    assert [c.open_time for c in out] == [T0 + i * H for i in range(10, 20)]


async def test_fill_gaps_refetches_missing() -> None:
    full = make_candles(20)
    ad = FakeAdapter(history=full)
    holed = full[:5] + full[9:]
    filled = await fill_gaps(ad, holed, "1h")
    assert [c.open_time for c in filled] == [c.open_time for c in full]


async def test_warm_up_returns_last_n() -> None:
    ad = FakeAdapter(history=make_candles(300))
    out = await warm_up(ad, "BTC/USDT", "1h", 50, now=T0 + 300 * H)
    assert len(out) == 50
    assert out[-1].open_time == T0 + 299 * H


def test_frame_roundtrip_decimal_exact() -> None:
    c = make_candles(3)
    back = frame_to_candles(candles_to_frame(c), "fake", "BTC/USDT", "1h")
    assert back == c


async def test_download_to_parquet_incremental(tmp_path: Path) -> None:
    ad = FakeAdapter(history=make_candles(100))
    path, n = await download_to_parquet(ad, "BTC/USDT", "1h", T0, tmp_path, until=T0 + 60 * H)
    assert n == 60
    assert path == parquet_path(tmp_path, "fake", "BTC/USDT", "1h")
    assert path.parts[-2:] == ("BTC-USDT", "1h.parquet")
    ad.ohlcv_calls.clear()
    _, n2 = await download_to_parquet(ad, "BTC/USDT", "1h", T0, tmp_path, until=T0 + 100 * H)
    assert n2 == 40
    assert ad.ohlcv_calls[0][0] == T0 + 60 * H  # resumed after the last stored candle
    df = load_ohlcv(path)
    assert len(df) == 100
    assert df.index.is_monotonic_increasing
    assert pd.DatetimeIndex(df.index).tz is not None
    assert not (tmp_path / "ohlcv" / "fake" / "BTC-USDT" / "1h.parquet.tmp").exists()


def test_utc_date() -> None:
    assert utc_date("2022-01-01") == datetime(2022, 1, 1, tzinfo=UTC)
    assert utc_date("2022-01-01T03:00:00+03:00") == datetime(2022, 1, 1, tzinfo=UTC)
