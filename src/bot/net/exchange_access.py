"""Startup reachability test from the egress IP (detects HTTP 451/403 restricted location)."""

from __future__ import annotations

from dataclasses import dataclass

from bot.net.egress import EgressHttpSession
from bot.net.errors import EgressUnavailableError, ErrorKind, classify_http_error

# Public, unauthenticated ping endpoints.
PING_URLS: dict[str, dict[bool, str]] = {
    "binance": {
        False: "https://api.binance.com/api/v3/ping",
        True: "https://testnet.binance.vision/api/v3/ping",
    },
    "binance_futures": {
        False: "https://fapi.binance.com/fapi/v1/ping",
        True: "https://demo-fapi.binance.com/fapi/v1/ping",
    },
    "btcturk": {
        False: "https://api.btcturk.com/api/v2/server/exchangeinfo",
        True: "https://api.btcturk.com/api/v2/server/exchangeinfo",
    },
}


@dataclass(frozen=True, slots=True)
class AccessResult:
    exchange: str
    url: str
    ok: bool
    kind: ErrorKind | None = None
    status: int | None = None

    @property
    def message(self) -> str:
        if self.ok:
            return f"{self.exchange}: erişim tamam."
        if self.kind is ErrorKind.RESTRICTED_LOCATION:
            return (
                f"{self.exchange}: 'restricted location' (HTTP {self.status}). Çıkış IP'sinin "
                "ülkesi bu borsa tarafından desteklenmiyor; VPS bölgesini değiştirin."
            )
        return f"{self.exchange}: erişim başarısız ({self.kind}, HTTP {self.status})."


def ping_url(exchange: str, market_type: str, testnet: bool) -> str:
    key = "binance_futures" if exchange == "binance" and market_type == "futures" else exchange
    if key not in PING_URLS:
        raise KeyError(f"Erişim testi tanımlı değil: {exchange}")
    return PING_URLS[key][testnet]


async def check_exchange_access(
    session: EgressHttpSession, exchange: str, url: str
) -> AccessResult:
    try:
        status, body = await session.request_text("GET", url)
    except EgressUnavailableError:
        return AccessResult(exchange, url, ok=False, kind=ErrorKind.NETWORK)
    if status < 400:
        return AccessResult(exchange, url, ok=True, status=status)
    return AccessResult(
        exchange, url, ok=False, kind=classify_http_error(status, body), status=status
    )
