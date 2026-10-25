from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from bot.config.schema import AppConfig, Mode
from bot.core.event_bus import EventBus
from bot.core.events import CandleEvent, SignalEvent
from bot.core.models import Candle, Horizon, PositionSide, Signal, TradePlan
from bot.ops.shadow import LIVE, SHADOW, ShadowBook, ShadowRegistry, ShadowRunner

T = datetime(2026, 1, 1, tzinfo=UTC)


def cfg(fast: int = 9, rsi: bool = False) -> AppConfig:
    return AppConfig.model_validate(
        {
            "strategies": {
                "ema_crossover": {"enabled": True, "fast": fast, "slow": 21, "timeframe": "1h"},
                "rsi_reversion": {"enabled": rsi, "timeframe": "1h"},
            }
        }
    )


def test_shadow_off_in_paper_and_baseline_on_first_live_start() -> None:
    reg = ShadowRegistry()
    assert reg.plan(cfg(), Mode.PAPER, T).shadow_strategies == {}
    assert reg.approved == {}
    plan = reg.plan(cfg(), Mode.LIVE, T)
    assert plan.shadow_strategies == {} and "temel" in plan.notes[0]
    assert set(reg.approved) == {"ema_crossover", "rsi_reversion"}


def test_changed_params_run_shadow_live_keeps_approved() -> None:
    reg = ShadowRegistry()
    reg.plan(cfg(fast=9), Mode.LIVE, T)
    plan = reg.plan(cfg(fast=5), Mode.LIVE, T)
    assert plan.shadow_names == {"ema_crossover"}
    assert (plan.live_config.strategies["ema_crossover"].model_extra or {})["fast"] == 9
    assert (plan.shadow_strategies["ema_crossover"].model_extra or {})["fast"] == 5
    # approval promotes the new params (next start)
    reg.approve("ema_crossover", cfg(fast=5), "ali", T)
    assert reg.plan(cfg(fast=5), Mode.LIVE, T).shadow_names == set()
    assert reg.history[-1]["by"] == "ali"


def test_new_strategy_is_shadow_only_and_disable_is_immediate() -> None:
    reg = ShadowRegistry.from_dict({"approved": {}})
    reg.approved = {"ema_crossover": dict(cfg().strategies["ema_crossover"].model_dump())}
    plan = reg.plan(cfg(rsi=True), Mode.TESTNET, T)
    assert "rsi_reversion" in plan.shadow_names
    assert "rsi_reversion" not in plan.live_config.strategies
    off = AppConfig.model_validate({"strategies": {"ema_crossover": {"enabled": False}}})
    plan = reg.plan(off, Mode.LIVE, T)
    assert plan.shadow_names == set()
    assert not plan.live_config.strategies["ema_crossover"].enabled
    assert ShadowRegistry.from_dict(reg.to_dict()).approved == reg.approved


def sig(strategy: str, ts: datetime, stop: str = "95", tp: str = "110") -> Signal:
    plan = TradePlan(
        symbol="BTC/USDT",
        exchange="binance",
        side=PositionSide.LONG,
        horizon=Horizon.SWING,
        timeframe="1h",
        entry_low=D(99),
        entry_high=D(101),
        stop_loss=D(stop),
        take_profits=(D(105), D(tp)),
        confluence_score=D(80),
        invalidation="x",
        created_at=ts,
    )
    return Signal(
        strategy=strategy,
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="1h",
        side=PositionSide.LONG,
        score=D(80),
        timestamp=ts,
        plan=plan,
    )


def bar(i: int, lo: str, hi: str) -> Candle:
    return Candle(
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="1h",
        open_time=T + timedelta(hours=i),
        open=D(100),
        high=D(hi),
        low=D(lo),
        close=D(100),
        volume=D(1),
    )


def test_virtual_book_r_multiples_and_compare() -> None:
    book = ShadowBook(fee_pct=D(0))
    book.on_signal(LIVE, sig("s", T + timedelta(hours=1)))
    book.on_signal(SHADOW, sig("s", T + timedelta(hours=1)))
    book.on_candle(bar(0, "90", "120"))  # before the signal: ignored
    assert len(book.open) == 2
    book.on_candle(bar(1, "99", "111"))  # TP hit
    book.on_signal(LIVE, sig("s", T + timedelta(hours=2)))
    book.on_candle(bar(2, "94", "111"))  # both touched -> stop first (conservative)
    live, sh = book.stats(LIVE, "s"), book.stats(SHADOW, "s")
    assert sh.trades == 1 and sh.expectancy_r == 2.0  # (110-100)/5
    assert live.trades == 2 and live.total_r == 1.0 and live.win_rate == 0.5
    txt = book.compare_tr("s", 1)
    assert "daha iyi" in txt
    assert "yetersiz veri" in book.compare_tr("s", 5)
    again = ShadowBook.from_dict(book.to_dict(), D(0))
    assert again.stats(LIVE, "s") == live


async def test_runner_scores_live_signals_and_never_publishes() -> None:
    bus = EventBus()
    book = ShadowBook(fee_pct=D(0))
    ShadowRunner(bus, [], {"s"}, book)
    published: list[SignalEvent] = []

    async def spy(e: SignalEvent) -> None:
        published.append(e)

    bus.subscribe(SignalEvent, spy)
    task = asyncio.create_task(bus.run())
    await bus.publish(SignalEvent(signal=sig("s", T)))
    await bus.publish(SignalEvent(signal=sig("other", T)))
    await bus.publish(CandleEvent(candle=bar(0, "99", "111")))
    await bus.stop()
    await task
    assert book.stats(LIVE, "s").trades == 1
    assert book.stats(LIVE, "other").trades == 0
    assert len(published) == 2  # only the two we sent
