"""Live strategy runner: CandleEvent -> strategies -> SignalEvent (no orders here)."""

from __future__ import annotations

from collections.abc import Callable

from bot.core.event_bus import EventBus
from bot.core.events import CandleEvent, SignalEvent
from bot.core.models import Signal
from bot.log import get_logger
from bot.strategies.base import BaseStrategy

_log = get_logger(__name__)


class StrategyRunner:
    def __init__(
        self,
        bus: EventBus,
        strategies: list[BaseStrategy],
        allow: Callable[[BaseStrategy], bool] = lambda _s: True,
    ) -> None:
        self.bus = bus
        self.strategies = strategies
        self.allow = allow
        self.last_signals: list[Signal] = []
        bus.subscribe(CandleEvent, self.on_candle)

    async def on_candle(self, event: CandleEvent) -> None:
        c = event.candle
        if not c.closed:
            return
        for s in self.strategies:
            if s.symbol != c.symbol or s.timeframe != c.timeframe:
                continue
            s.on_candle(c)  # keep indicators warm even while disabled
            if not self.allow(s):
                continue
            try:
                signals = s.generate_signals()
            except Exception:
                _log.exception("strategy_failed", strategy=s.name, symbol=s.symbol)
                continue
            for sig in signals:
                self.last_signals.append(sig)
                self.last_signals = self.last_signals[-200:]
                await self.bus.publish(SignalEvent(signal=sig))
