"""Build the exchange adapters for the current mode.

- paper:   real public market data (no API keys) wrapped in `PaperExchange`
- testnet: testnet/demo endpoints with testnet keys; exchanges without a sandbox get public
           data only (no keys, no orders)
- live:    real endpoints with live keys (only reachable after the live double-confirmation)
- backtest: no live adapters
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

from bot.config import Mode, Settings
from bot.config.schema import ExchangeConfig
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.binance import BinanceAdapter
from bot.exchanges.btcturk import BtcturkAdapter
from bot.exchanges.errors import NotSupportedError
from bot.exchanges.generic import GenericCcxtAdapter
from bot.exchanges.paper import FillCallback, PaperExchange
from bot.exchanges.profiles import PROFILES, profile
from bot.net.egress import Egress

SUPPORTED = tuple(sorted(PROFILES))


@dataclass(frozen=True)
class Credentials:
    api_key: str | None = None
    secret: str | None = None
    password: str | None = None


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def _credentials(settings: Settings, name: str) -> Credentials:
    """Keys for the current mode. Paper / backtest never attach keys; testnet uses only the
    TESTNET_* keys of exchanges that have a sandbox; live only the live keys."""
    if settings.mode not in (Mode.TESTNET, Mode.LIVE):
        return Credentials()
    testnet = settings.mode is Mode.TESTNET
    if testnet and not profile(name).sandbox:
        return Credentials()
    found = settings.exchange_keys.get((name, testnet))
    if found is not None:
        return Credentials(
            found.api_key.get_secret_value(),
            found.secret.get_secret_value(),
            _secret(found.password),
        )
    s = settings.secrets  # explicit fields (kept for compatibility)
    if name == "binance":
        if testnet:
            return Credentials(
                _secret(s.binance_testnet_api_key), _secret(s.binance_testnet_api_secret)
            )
        return Credentials(_secret(s.binance_api_key), _secret(s.binance_api_secret))
    if name == "btcturk" and not testnet:
        return Credentials(_secret(s.btcturk_api_key), _secret(s.btcturk_api_secret))
    return Credentials()


def build_public_adapter(
    name: str, cfg: ExchangeConfig, egress: Egress, testnet: bool
) -> ExchangeAdapter:
    return _build(name, cfg, egress, testnet=testnet, creds=Credentials())


def _build(
    name: str, cfg: ExchangeConfig, egress: Egress, *, testnet: bool, creds: Credentials
) -> ExchangeAdapter:
    try:
        prof = profile(name)
    except KeyError as exc:
        raise NotSupportedError(str(exc.args[0])) from exc
    proxy = egress.ccxt_options()
    if name == "binance":
        return BinanceAdapter.create(
            market_type=cfg.market_type,
            testnet=testnet,
            proxy_options=proxy,
            api_key=creds.api_key,
            secret=creds.secret,
        )
    if name == "btcturk":
        return BtcturkAdapter.create(
            testnet=testnet, proxy_options=proxy, api_key=creds.api_key, secret=creds.secret
        )
    if prof.needs_password and creds.api_key and not creds.password:
        raise NotSupportedError(
            f"{prof.label} API passphrase ister: .env -> {prof.env_prefix}_"
            f"{'TESTNET_' if testnet else ''}API_PASSWORD"
        )
    return GenericCcxtAdapter.create(
        prof,
        market_type=cfg.market_type,
        testnet=testnet,
        proxy_options=proxy,
        api_key=creds.api_key,
        secret=creds.secret,
        password=creds.password,
    )


def build_exchanges(
    settings: Settings, egress: Egress, on_fill: FillCallback | None = None
) -> dict[str, ExchangeAdapter]:
    out: dict[str, ExchangeAdapter] = {}
    if settings.mode is Mode.BACKTEST:
        return out
    for name, cfg in settings.config.enabled_exchanges.items():
        if settings.mode is Mode.PAPER:
            source = _build(name, cfg, egress, testnet=False, creds=Credentials())
            out[name] = PaperExchange(
                name,
                data_source=source,
                initial_balances=dict(settings.config.paper.initial_balances),
                on_fill=on_fill,
            )
            continue
        testnet = settings.mode is Mode.TESTNET
        out[name] = _build(name, cfg, egress, testnet=testnet, creds=_credentials(settings, name))
    return out
