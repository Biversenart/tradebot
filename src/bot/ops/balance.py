"""Per-exchange balance ceiling (spec §5.13): spread exchange risk; alert to rebalance.

Transfers stay manual (CLAUDE.md rule 3); this only warns, repeating at most every
`repeat_hours` per exchange.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from bot.core.clock import Clock, SystemClock
from bot.core.events import AlertLevel
from bot.ops.alerts import AlertSink, emit


class ExchangeBalanceCap:
    def __init__(
        self,
        max_quote: Decimal,
        repeat_hours: float = 6,
        alert_sink: AlertSink | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.max_quote = max_quote
        self.repeat = timedelta(hours=repeat_hours)
        self.alert_sink = alert_sink
        self.clock = clock or SystemClock()
        self._last: dict[str, datetime] = {}

    async def check(self, per_exchange: dict[str, Decimal]) -> list[str]:
        over = [n for n, v in per_exchange.items() if v > self.max_quote]
        now = self.clock.now()
        for name in over:
            last = self._last.get(name)
            if last is not None and now - last < self.repeat:
                continue
            self._last[name] = now
            value = per_exchange[name]
            await emit(
                self.alert_sink,
                AlertLevel.WARNING,
                "exchange_balance_cap",
                f"{name} bakiyesi {value:.2f} > tavan {self.max_quote}; fazlasını başka borsaya "
                "manuel aktarmayı düşünün (borsa riski dağıtımı).",
                exchange=name,
                value=value,
            )
        for name in list(self._last):
            if name not in over:
                del self._last[name]
        return over
