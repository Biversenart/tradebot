"""Stablecoin depeg monitor (spec §5.13).

Watches stable/stable pairs (USDC/USDT, FDUSD/USDT, ...). A deviation above the threshold
blocks new trades and raises a CRITICAL alert; it clears below half the threshold (hysteresis).
Missing prices never block trading but are reported once.
"""

from __future__ import annotations

from decimal import Decimal

from bot.core.events import AlertLevel
from bot.ops.alerts import AlertSink, emit

ONE = Decimal(1)


class DepegMonitor:
    def __init__(
        self, threshold_pct: Decimal, pairs: tuple[str, ...], alert_sink: AlertSink | None = None
    ) -> None:
        self.threshold = threshold_pct
        self.pairs = pairs
        self.alert_sink = alert_sink
        self.depegged: dict[str, Decimal] = {}
        self._missing_reported: set[str] = set()

    async def update(self, prices: dict[str, Decimal]) -> None:
        for pair in self.pairs:
            px = prices.get(pair)
            if px is None or px <= 0:
                if pair not in self._missing_reported:
                    self._missing_reported.add(pair)
                    await emit(
                        self.alert_sink,
                        AlertLevel.INFO,
                        "stablecoin_price_missing",
                        f"{pair} fiyatı yok; depeg kontrolü bu parite için çalışmıyor.",
                    )
                continue
            self._missing_reported.discard(pair)
            dev = abs(px - ONE) * 100
            if dev > self.threshold and pair not in self.depegged:
                self.depegged[pair] = px
                await emit(
                    self.alert_sink,
                    AlertLevel.CRITICAL,
                    "stablecoin_depeg",
                    f"{pair} = {px} (sapma %{dev:.2f} > %{self.threshold}); yeni işlemler durdu.",
                    pair=pair,
                    price=px,
                )
            elif pair in self.depegged and dev <= self.threshold / 2:
                del self.depegged[pair]
                await emit(
                    self.alert_sink,
                    AlertLevel.INFO,
                    "stablecoin_depeg_recovered",
                    f"{pair} = {px}; sabitlik geri geldi.",
                    pair=pair,
                )
            elif pair in self.depegged:
                self.depegged[pair] = px

    def block_reason(self) -> str | None:
        if not self.depegged:
            return None
        return "stablecoin depeg: " + ", ".join(f"{p}={v}" for p, v in self.depegged.items())
