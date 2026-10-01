"""Build the exchange adapters for the current mode.

- paper:   real public market data (no API keys) wrapped in `PaperExchange`
- testnet: testnet/demo endpoints with testnet keys (BTCTurk: public data only, no orders)
- live:    real endpoints with live keys (only reachable after the live double-confirmation)
- backtest: no live adapters
"""

from __future__ import annotations

from pydantic import SecretStr

from bot.config import Mode, Settings
from bot.config.schema import ExchangeConfig
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.binance import BinanceAdapter
from bot.exchanges.btcturk import BtcturkAdapter
from bot.exchanges.errors import NotSupportedError
from bot.exchanges.paper import FillCallback, PaperExchange
from bot.net.egress import Egress

SUPPORTED = ("binance", "btcturk")


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def _credentials(settings: Settings, name: str) -> tuple[str | None, str | None]:
    s = settings.secrets
    if settings.mode is Mode.LIVE:
        if name == "binance":
            return _secret(s.binance_api_key), _secret(s.binance_api_secret)
        if name == "btcturk":
            return _secret(s.btcturk_api_key), _secret(s.btcturk_api_secret)
    if settings.mode is Mode.TESTNET and name == "binance":
        return _secret(s.binance_testnet_api_key), _secret(s.binance_testnet_api_secret)
    return None, None  # paper / backtest / BTCTurk testnet: never attach keys


def build_public_adapter(
    name: str, cfg: ExchangeConfig, egress: Egress, testnet: bool
) -> ExchangeAdapter:
    return _build(name, cfg, egress, testnet=testnet, api_key=None, secret=None)


def _build(
    name: str,
    cfg: ExchangeConfig,
    egress: Egress,
    *,
    testnet: bool,
    api_key: str | None,
    secret: str | None,
) -> ExchangeAdapter:
    proxy = egress.ccxt_options()
    if name == "binance":
        return BinanceAdapter.create(
            market_type=cfg.market_type,
            testnet=testnet,
            proxy_options=proxy,
            api_key=api_key,
            secret=secret,
        )
    if name == "btcturk":
        return BtcturkAdapter.create(
            testnet=testnet, proxy_options=proxy, api_key=api_key, secret=secret
        )
    raise NotSupportedError(f"Desteklenmeyen borsa: {name} (desteklenen: {', '.join(SUPPORTED)})")


def build_exchanges(
    settings: Settings, egress: Egress, on_fill: FillCallback | None = None
) -> dict[str, ExchangeAdapter]:
    out: dict[str, ExchangeAdapter] = {}
    if settings.mode is Mode.BACKTEST:
        return out
    for name, cfg in settings.config.enabled_exchanges.items():
        if settings.mode is Mode.PAPER:
            source = _build(name, cfg, egress, testnet=False, api_key=None, secret=None)
            out[name] = PaperExchange(
                name,
                data_source=source,
                initial_balances=dict(settings.config.paper.initial_balances),
                on_fill=on_fill,
            )
            continue
        key, secret = _credentials(settings, name)
        testnet = settings.mode is Mode.TESTNET
        out[name] = _build(name, cfg, egress, testnet=testnet, api_key=key, secret=secret)
    return out
