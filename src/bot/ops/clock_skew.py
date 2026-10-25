"""Exchange clock skew monitor (spec §5.13 chaos: saat kayması).

Signed requests fail (Binance -1021) and candle timing drifts when the local clock is off.
ccxt's adjustForTimeDifference compensates; beyond `max_clock_skew_ms` new orders on that
exchange are blocked until the offset is back within half the limit.
"""

from __future__ import annotations

from collections.abc import Mapping

from bot.core.events import AlertLevel
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import ExchangeAdapterError
from bot.log import get_logger
from bot.ops.alerts import AlertSink, emit

_log = get_logger(__name__)


class ClockSkewMonitor:
    def __init__(self, max_skew_ms: int, alert_sink: AlertSink | None = None) -> None:
        self.max_ms = max_skew_ms
        self.alert_sink = alert_sink
        self.skewed: dict[str, float] = {}
        self.offsets: dict[str, float] = {}

    async def check(self, adapters: Mapping[str, ExchangeAdapter]) -> None:
        for name, ad in adapters.items():
            try:
                off_ms = (await ad.server_time_offset()).total_seconds() * 1000
            except ExchangeAdapterError as exc:
                _log.warning("clock_check_failed", exchange=name, error=str(exc))
                continue
            self.offsets[name] = off_ms
            if abs(off_ms) > self.max_ms and name not in self.skewed:
                self.skewed[name] = off_ms
                await emit(
                    self.alert_sink,
                    AlertLevel.WARNING,
                    "clock_skew",
                    f"{name}: saat farkı {off_ms:.0f} ms > {self.max_ms} ms; yeni emirler durdu. "
                    "Sistem saatini (NTP) kontrol edin.",
                    exchange=name,
                )
            elif name in self.skewed and abs(off_ms) <= self.max_ms / 2:
                del self.skewed[name]
                await emit(
                    self.alert_sink,
                    AlertLevel.INFO,
                    "clock_skew_recovered",
                    f"{name}: saat farkı {off_ms:.0f} ms; normale döndü.",
                    exchange=name,
                )

    def block_reason(self, exchange: str) -> str | None:
        off = self.skewed.get(exchange)
        return None if off is None else f"saat kayması {off:.0f} ms ({exchange})"
