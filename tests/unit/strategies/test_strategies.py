from __future__ import annotations

import asyncio
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from bot.config.schema import AppConfig
from bot.core.event_bus import EventBus
from bot.core.events import CandleEvent, SignalEvent
from bot.core.models import Candle, PositionSide
from bot.strategies.base import BaseStrategy, assert_causal
from bot.strategies.combiner import SignalCombiner
from bot.strategies.registry import STRATEGIES, build_enabled, build_strategy
from bot.strategies.runner import StrategyRunner
from tests.fixtures.loader import synthetic_1h

DF = synthetic_1h()
CFG = AppConfig()


def make(name: str, **params: object) -> BaseStrategy:
    return build_strategy(CFG, name, "BTC/USDT", overrides=dict(params))


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_compute_is_causal(name: str) -> None:
    s = make(name)
    assert_causal(s, DF, [s.warmup_bars + 5, 600, 801, 1203, 1450, len(DF)])


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_compute_shape_and_values(name: str) -> None:
    out = make(name).compute(DF)
    assert list(out.index) == list(DF.index)
    assert set(out["signal"].unique()) <= {-1, 0, 1}
    sig = out[out["signal"] != 0]
    assert ((sig["score"] >= 0) & (sig["score"] <= 100)).all()
    assert (sig["reason"] != "").all()


def regime_share(out: pd.DataFrame, lo: int, hi: int) -> int:
    return int((out["signal"].iloc[lo:hi] != 0).sum())


def test_regime_gating() -> None:
    ema = make("ema_crossover").compute(DF)
    assert regime_share(ema, 450, 780) == 0  # range segment: trend strategy silent
    rsi = make("rsi_reversion").compute(DF)
    assert regime_share(rsi, 260, 400) == 0  # strong uptrend: mean reversion silent
    grid = make("grid").compute(DF)
    assert regime_share(grid, 1000, 1200) == 0  # downtrend: grid silent
    assert (grid["signal"] >= 0).all()  # long only
    dca = make("dca").compute(DF)
    assert regime_share(dca, 1000, 1200) == 0  # no DCA into a strong downtrend


def test_signal_from_row_builds_valid_plan() -> None:
    s = make("breakout")
    out = s.compute(DF)
    i = int(np.flatnonzero(out["signal"].to_numpy() != 0)[0])
    sig = s.signal_from_row(DF.index[i], float(DF["close"].iloc[i]), out.iloc[i])
    assert sig is not None and sig.plan is not None
    p = sig.plan
    assert float(p.risk_reward) == pytest.approx(3.0, rel=1e-4)
    assert p.timeframe == "1h"
    if p.side is PositionSide.LONG:
        assert p.stop_loss < p.entry_low
    assert sig.timestamp > DF.index[i].to_pydatetime()  # stamped at bar close


def test_live_path_equals_backtest_path() -> None:
    s = make("breakout")
    full = s.compute(DF)
    live = make("breakout")
    hits = 0
    for ts, row in DF.iterrows():
        live.on_candle(
            Candle(
                exchange="binance",
                symbol="BTC/USDT",
                timeframe="1h",
                open_time=pd.Timestamp(str(ts)).to_pydatetime(),
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=Decimal(str(row.volume)),
            )
        )
        if len(live._rows) >= live.warmup_bars and ts in full.index[full["signal"] != 0]:
            sigs = live.generate_signals()
            assert sigs, f"live missed signal at {ts}"
            hits += 1
            if hits >= 3:
                break
    assert hits >= 1


def test_on_candle_filters() -> None:
    s = make("ema_crossover")
    c = Candle(
        exchange="binance",
        symbol="ETH/USDT",
        timeframe="1h",
        open_time=pd.Timestamp("2024-01-01", tz="UTC").to_pydatetime(),
        open=Decimal(1),
        high=Decimal(2),
        low=Decimal(1),
        close=Decimal(2),
        volume=Decimal(1),
    )
    s.on_candle(c)  # other symbol
    s.on_candle(c.model_copy(update={"symbol": "BTC/USDT", "closed": False}))  # not closed
    assert s.generate_signals() == [] and not s._rows


def test_params_validated() -> None:
    with pytest.raises(ValidationError):
        make("ema_crossover", fastt=3)
    with pytest.raises(KeyError):
        build_strategy(CFG, "martingale", "BTC/USDT")


def test_build_enabled_from_example_config() -> None:
    from pathlib import Path

    from bot.config import load_app_config

    cfg = load_app_config(Path(__file__).resolve().parents[3] / "config" / "config.example.yaml")
    names = [s.name for s in build_enabled(cfg, "BTC/USDT")]
    assert names == ["ema_crossover", "rsi_reversion", "breakout"]
    cfg2 = cfg.model_copy(
        update={
            "strategies": {
                **cfg.strategies,
                "signal_combiner": cfg.strategies["signal_combiner"].model_copy(
                    update={"enabled": True}
                ),
            }
        }
    )
    combined = build_enabled(cfg2, "BTC/USDT")
    assert len(combined) == 1 and isinstance(combined[0], SignalCombiner)


def test_combiner_votes() -> None:
    members = [make("breakout"), make("ema_crossover")]
    comb = SignalCombiner(
        members, "BTC/USDT", params={"threshold": 0.3, "weights": {"breakout": 2}}
    )
    out = comb.compute(DF)
    b = members[0].compute(DF)
    assert (
        (out["signal"] != 0) <= (b["signal"] != 0) | (members[1].compute(DF)["signal"] != 0)
    ).all()
    assert (out.loc[out["signal"] != 0, "reason"].str.startswith("oy birliği")).all()
    assert_causal(comb, DF, [700, 1300])
    with pytest.raises(ValueError):
        SignalCombiner([], "BTC/USDT")


async def test_runner_publishes_signals() -> None:
    bus = EventBus()
    s = make("dca", interval_bars=1)
    runner = StrategyRunner(bus, [s])
    got: list[SignalEvent] = []

    async def on_sig(e: SignalEvent) -> None:
        got.append(e)

    bus.subscribe(SignalEvent, on_sig)
    task = asyncio.create_task(bus.run())
    for ts, row in DF.iloc[:700].iterrows():
        await bus.publish(
            CandleEvent(
                candle=Candle(
                    exchange="binance",
                    symbol="BTC/USDT",
                    timeframe="1h",
                    open_time=pd.Timestamp(str(ts)).to_pydatetime(),
                    open=Decimal(str(row.open)),
                    high=Decimal(str(row.high)),
                    low=Decimal(str(row.low)),
                    close=Decimal(str(row.close)),
                    volume=Decimal(str(row.volume)),
                )
            )
        )
    await bus.stop()
    await asyncio.wait_for(task, 30)
    assert got and all(e.signal.strategy == "dca" for e in got)
    assert runner.last_signals
