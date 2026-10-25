"""Egress configuration: every request to an exchange leaves through the fixed-IP egress.

- `proxy` / `managed_proxy`: HTTP(S)/SOCKS proxy; REST and WebSocket both use it.
- `wireguard` / `direct_vps`: routing is done by the OS / container network namespace
  (WireGuard kill-switch in docker-compose); no proxy settings are injected.

There is deliberately NO code path that builds a direct client when a proxy is configured
or required: if the proxy is down, requests fail (fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp_socks import ProxyConnectionError, ProxyConnector, ProxyError, ProxyTimeoutError
from pydantic import SecretStr

from bot.config import EgressMode, Settings
from bot.log import get_logger
from bot.net.errors import EgressMisconfiguredError, EgressUnavailableError

_log = get_logger(__name__)

SOCKS_SCHEMES = frozenset({"socks4", "socks5", "socks5h"})
HTTP_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class ProxyEndpoint:
    scheme: str
    host: str
    port: int
    url: SecretStr  # full URL incl. credentials; never log

    @property
    def is_socks(self) -> bool:
        return self.scheme in SOCKS_SCHEMES

    def __repr__(self) -> str:  # never leak credentials
        return f"ProxyEndpoint({self.scheme}://{self.host}:{self.port})"

    __str__ = __repr__


def parse_proxy_url(url: SecretStr) -> ProxyEndpoint:
    raw = url.get_secret_value().strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in SOCKS_SCHEMES | HTTP_SCHEMES:
        raise EgressMisconfiguredError(
            f"Desteklenmeyen proxy şeması: {scheme or '-'} (http, https, socks4, socks5, socks5h)"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise EgressMisconfiguredError("Proxy portu geçersiz.") from exc
    if not parts.hostname or port is None:
        raise EgressMisconfiguredError("Proxy URL'sinde host ve port zorunlu.")
    return ProxyEndpoint(scheme=scheme, host=parts.hostname, port=port, url=SecretStr(raw))


@dataclass(frozen=True, slots=True)
class Egress:
    """Resolved egress for the current run."""

    mode: EgressMode
    proxy: ProxyEndpoint | None
    expected_ip: str | None
    timeout_seconds: float = 10.0

    @classmethod
    def from_settings(cls, settings: Settings) -> Egress:
        cfg = settings.config.egress
        url = settings.secrets.egress_proxy_url
        proxy: ProxyEndpoint | None = None
        if cfg.mode.requires_proxy_url:
            if url is None:
                raise EgressMisconfiguredError(
                    f"egress.mode={cfg.mode} için EGRESS_PROXY_URL zorunlu."
                )
            proxy = parse_proxy_url(url)
        elif url is not None:
            # Tunnel / VPS modes route at the OS level; a stray proxy URL is ignored.
            _log.warning("egress_proxy_url_ignored", egress_mode=str(cfg.mode))
        return cls(
            mode=cfg.mode,
            proxy=proxy,
            expected_ip=settings.secrets.egress_expected_ip,
            timeout_seconds=float(cfg.request_timeout_seconds),
        )

    def ccxt_options(self) -> dict[str, Any]:
        """Proxy options for ccxt / ccxt.pro (REST + WebSocket through the same proxy)."""
        if self.proxy is None:
            return {}
        url = self.proxy.url.get_secret_value()
        if self.proxy.is_socks:
            return {"socksProxy": url, "wsSocksProxy": url}
        return {"httpsProxy": url, "wssProxy": url}

    def new_http_session(self) -> EgressHttpSession:
        return EgressHttpSession(self)


def resolve_egress(settings: Settings) -> Egress:
    """Egress for this run. testnet/live: strict (errors propagate, fail closed).

    paper/backtest send no real orders: the proxy is used when configured, otherwise
    traffic goes out directly (warning logged).
    """
    if settings.sends_real_orders:
        return Egress.from_settings(settings)
    try:
        return Egress.from_settings(settings)
    except EgressMisconfiguredError:
        _log.warning("paper_mode_without_egress_proxy")
        return Egress(
            mode=settings.config.egress.mode,
            proxy=None,
            expected_ip=settings.secrets.egress_expected_ip,
            timeout_seconds=float(settings.config.egress.request_timeout_seconds),
        )


class EgressHttpSession:
    """aiohttp session that always routes through the egress (if one is configured).

    Errors reaching the proxy surface as `EgressUnavailableError`; nothing is retried
    without the proxy.
    """

    def __init__(self, egress: Egress) -> None:
        self._egress = egress
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> EgressHttpSession:
        self._ensure_session()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._egress.timeout_seconds)
            proxy = self._egress.proxy
            if proxy is not None and proxy.is_socks:
                connector = ProxyConnector.from_url(proxy.url.get_secret_value(), rdns=True)
                self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
            else:
                # trust_env=False: ignore HTTP(S)_PROXY env vars; the egress decides.
                self._session = aiohttp.ClientSession(timeout=timeout, trust_env=False)
        return self._session

    async def request_text(self, method: str, url: str) -> tuple[int, str]:
        session = self._ensure_session()
        proxy = self._egress.proxy
        http_proxy = proxy.url.get_secret_value() if proxy and not proxy.is_socks else None
        try:
            async with session.request(method, url, proxy=http_proxy) as resp:
                return resp.status, await resp.text()
        except (
            aiohttp.ClientError,
            OSError,
            TimeoutError,
            ProxyError,
            ProxyConnectionError,
            ProxyTimeoutError,
        ) as exc:
            # Message intentionally excludes the proxy URL (may contain credentials).
            raise EgressUnavailableError(
                f"Egress üzerinden istek başarısız ({type(exc).__name__}); "
                "doğrudan bağlantıya geri dönülmedi."
            ) from exc

    async def get_text(self, url: str) -> str:
        status, body = await self.request_text("GET", url)
        if status >= 400:
            raise EgressUnavailableError(f"{url} HTTP {status} döndü.")
        return body

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
