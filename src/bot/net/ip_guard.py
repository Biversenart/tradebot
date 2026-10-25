"""External IP verification (fail-closed).

Orders are allowed only while the most recent check succeeded, is fresh, and EVERY
configured service (at least two, independent) reported exactly the expected IP.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from bot.core.aio import wait_or_stop
from bot.core.clock import Clock, SystemClock
from bot.core.events import AlertLevel, RiskAlert
from bot.log import get_logger
from bot.net.errors import EgressBlockedError

IpFetcher = Callable[[str], Awaitable[str]]
AlertSink = Callable[[RiskAlert], Awaitable[None]]

_log = get_logger(__name__)


class IpStatus(StrEnum):
    UNKNOWN = "unknown"
    OK = "ok"
    MISMATCH = "mismatch"
    INCONSISTENT = "inconsistent"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class IpCheckResult:
    status: IpStatus
    checked_at: datetime
    observed: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is IpStatus.OK


def _normalize_ip(text: str) -> str:
    return str(ipaddress.ip_address(text.strip()))


class IpGuard:
    def __init__(
        self,
        expected_ip: str,
        services: Sequence[str],
        fetch_ip: IpFetcher,
        check_interval: timedelta,
        clock: Clock | None = None,
        alert_sink: AlertSink | None = None,
    ) -> None:
        if len(set(services)) < 2:
            raise ValueError("En az 2 bağımsız IP servisi gerekli.")
        self.expected_ip = _normalize_ip(expected_ip)
        self._services = tuple(services)
        self._fetch_ip = fetch_ip
        self._interval = check_interval
        self._clock = clock or SystemClock()
        self._alert_sink = alert_sink
        self._last: IpCheckResult | None = None
        self._last_alerted: IpStatus = IpStatus.UNKNOWN

    @property
    def last_result(self) -> IpCheckResult | None:
        return self._last

    @property
    def status(self) -> IpStatus:
        return self._last.status if self._last else IpStatus.UNKNOWN

    def is_stale(self) -> bool:
        if self._last is None:
            return True
        return self._clock.now() - self._last.checked_at > self._interval * 2

    def allows_orders(self) -> bool:
        """Fail-closed: unknown, failed or stale verification blocks orders."""
        return self._last is not None and self._last.ok and not self.is_stale()

    def ensure_orders_allowed(self) -> None:
        if not self.allows_orders():
            raise EgressBlockedError(
                f"Dış IP doğrulanmadı (durum: {self.status}, bayat: {self.is_stale()}); "
                "emir gönderilmiyor."
            )

    async def _query(self, service: str) -> tuple[str, str | None, str | None]:
        try:
            raw = await self._fetch_ip(service)
            return service, _normalize_ip(raw), None
        except ValueError:
            return service, None, "geçersiz yanıt"
        except Exception as exc:  # any failure => unverified (fail-closed)
            return service, None, type(exc).__name__

    async def check(self) -> IpCheckResult:
        results = await asyncio.gather(*(self._query(s) for s in self._services))
        observed = {s: ip for s, ip, _ in results if ip is not None}
        errors = {s: err for s, _, err in results if err is not None}
        if errors:
            status = IpStatus.UNREACHABLE
        elif len(set(observed.values())) > 1:
            status = IpStatus.INCONSISTENT
        elif set(observed.values()) != {self.expected_ip}:
            status = IpStatus.MISMATCH
        else:
            status = IpStatus.OK
        result = IpCheckResult(status, self._clock.now(), observed, errors)
        self._last = result
        await self._report(result)
        return result

    async def _report(self, result: IpCheckResult) -> None:
        if result.status is self._last_alerted:
            return
        previous = self._last_alerted
        self._last_alerted = result.status
        if result.ok:
            if previous is IpStatus.UNKNOWN:
                _log.info("egress_ip_verified", ip=self.expected_ip)
                return
            alert = RiskAlert(
                level=AlertLevel.INFO,
                code="egress_ip_recovered",
                message="Dış IP yeniden doğrulandı; emirlere izin veriliyor.",
            )
        else:
            alert = RiskAlert(
                level=AlertLevel.CRITICAL,
                code=f"egress_ip_{result.status}",
                message=(
                    f"Dış IP doğrulaması başarısız ({result.status}). Yeni emirler durduruldu."
                ),
                details={
                    "expected": self.expected_ip,
                    "observed": dict(result.observed),
                    "errors": dict(result.errors),
                },
            )
        _log.warning("egress_ip_status_changed", status=str(result.status), code=alert.code)
        if self._alert_sink is not None:
            await self._alert_sink(alert)

    async def run_periodic(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.check()
            except Exception:  # never let the guard die silently; stay fail-closed
                self._last = None
                _log.exception("egress_ip_check_crashed")
            await wait_or_stop(stop, self._interval.total_seconds())
