"""Exchange profiles: what each supported exchange can do and how the bot talks to it.

Binance and BTCTurk keep their dedicated adapters; every other exchange runs on
`GenericCcxtAdapter` driven by its profile. Facts below were taken from ccxt's capability
flags (`has`, `urls['test']`, `requiredCredentials`) plus each exchange's API rules for client
order ids. They are verified offline by tests against the installed ccxt; real order
placement must still be verified per exchange on testnet / with a tiny live order.

Safety (CLAUDE.md rule 9): an exchange without exchange-side stop orders
(`exchange_stop=False`) never gets strategy positions — it is used for market data,
arbitrage scanning and analysis only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ClientIdFormat = Literal["plain", "gate", "uuid", "int"]
StopParam = Literal["stopLossPrice", "triggerPrice"]


@dataclass(frozen=True)
class ExchangeProfile:
    name: str  # config key (config.yaml `exchanges:` and env prefix)
    ccxt_id: str
    label: str
    sandbox: bool  # ccxt sandbox / demo endpoints exist -> testnet orders possible
    exchange_stop: bool  # stop orders can rest on the exchange (rule 9)
    stop_param: StopParam = "stopLossPrice"
    needs_password: bool = False  # API passphrase (OKX, KuCoin, Bitget, ...)
    client_id: ClientIdFormat = "plain"
    websocket: bool = True
    quote: str = "USDT"  # main quote currency for symbols on this exchange
    ping: str = ""  # public endpoint for the restricted-location check
    ping_test: str = ""
    futures: bool = False  # bot supports futures here (only Binance for now)
    note: str = ""

    @property
    def env_prefix(self) -> str:
        return self.name.upper()


PROFILES: dict[str, ExchangeProfile] = {
    p.name: p
    for p in (
        ExchangeProfile(
            "binance",
            "binance",
            "Binance",
            sandbox=True,
            exchange_stop=True,
            ping="https://api.binance.com/api/v3/ping",
            ping_test="https://testnet.binance.vision/api/v3/ping",
            futures=True,
            note="Ana borsa; spot + USDⓈ-M futures (demo).",
        ),
        ExchangeProfile(
            "coinbase",
            "coinbase",
            "Coinbase (Advanced Trade)",
            sandbox=False,
            exchange_stop=True,
            stop_param="triggerPrice",
            quote="USDC",
            ping="https://api.coinbase.com/api/v3/brokerage/time",
            note="API anahtarı: CDP key name + private key (secret).",
        ),
        ExchangeProfile(
            "okx",
            "okx",
            "OKX",
            sandbox=True,
            exchange_stop=True,
            needs_password=True,
            ping="https://www.okx.com/api/v5/public/time",
            ping_test="https://www.okx.com/api/v5/public/time",
            note="Demo trading anahtarı ayrıca oluşturulur (testnet).",
        ),
        ExchangeProfile(
            "bybit",
            "bybit",
            "Bybit",
            sandbox=True,
            exchange_stop=True,
            ping="https://api.bybit.com/v5/market/time",
            ping_test="https://api-testnet.bybit.com/v5/market/time",
        ),
        ExchangeProfile(
            "upbit",
            "upbit",
            "Upbit",
            sandbox=False,
            exchange_stop=False,
            quote="KRW",
            ping="https://api.upbit.com/v1/market/all",
            note="KRW piyasası; Kore'de yerleşik hesap gerekir. Borsada stop yok.",
        ),
        ExchangeProfile(
            "kraken",
            "kraken",
            "Kraken",
            sandbox=False,
            exchange_stop=True,
            stop_param="triggerPrice",
            client_id="uuid",
            quote="USD",
            ping="https://api.kraken.com/0/public/Time",
        ),
        ExchangeProfile(
            "bitget",
            "bitget",
            "Bitget",
            sandbox=False,
            exchange_stop=True,
            needs_password=True,
            ping="https://api.bitget.com/api/v2/public/time",
        ),
        ExchangeProfile(
            "gate",
            "gate",
            "Gate",
            sandbox=True,
            exchange_stop=True,
            client_id="gate",
            ping="https://api.gateio.ws/api/v4/spot/time",
            ping_test="https://api-testnet.gateapi.io/api/v4/spot/time",
        ),
        ExchangeProfile(
            "kucoin",
            "kucoin",
            "KuCoin",
            sandbox=False,
            exchange_stop=True,
            needs_password=True,
            ping="https://api.kucoin.com/api/v1/timestamp",
        ),
        ExchangeProfile(
            "htx",
            "htx",
            "HTX",
            sandbox=False,
            exchange_stop=True,
            ping="https://api.huobi.pro/v1/common/timestamp",
        ),
        ExchangeProfile(
            "mexc",
            "mexc",
            "MEXC",
            sandbox=False,
            exchange_stop=True,
            stop_param="triggerPrice",
            ping="https://api.mexc.com/api/v3/ping",
        ),
        ExchangeProfile(
            "cryptocom",
            "cryptocom",
            "Crypto.com Exchange",
            sandbox=True,
            exchange_stop=True,
            stop_param="triggerPrice",
            ping="https://api.crypto.com/exchange/v1/public/get-instruments",
            ping_test="https://uat-api.3ona.co/exchange/v1/public/get-instruments",
        ),
        ExchangeProfile(
            "bitfinex",
            "bitfinex",
            "Bitfinex",
            sandbox=False,
            exchange_stop=True,
            stop_param="triggerPrice",
            client_id="int",
            quote="USD",
            ping="https://api-pub.bitfinex.com/v2/platform/status",
        ),
        ExchangeProfile(
            "bitstamp",
            "bitstamp",
            "Bitstamp",
            sandbox=False,
            exchange_stop=False,
            quote="USD",
            ping="https://www.bitstamp.net/api/v2/trading-pairs-info/",
            note="Borsada stop emri yok.",
        ),
        ExchangeProfile(
            "gemini",
            "gemini",
            "Gemini",
            sandbox=True,
            exchange_stop=False,
            quote="USD",
            ping="https://api.gemini.com/v1/symbols",
            ping_test="https://api.sandbox.gemini.com/v1/symbols",
            note="Borsada stop emri yok (ccxt).",
        ),
        ExchangeProfile(
            "bingx",
            "bingx",
            "BingX",
            sandbox=True,
            exchange_stop=True,
            ping="https://open-api.bingx.com/openApi/spot/v1/server/time",
            ping_test="https://open-api-vst.bingx.com/openApi/spot/v1/server/time",
        ),
        ExchangeProfile(
            "bitvavo",
            "bitvavo",
            "Bitvavo",
            sandbox=False,
            exchange_stop=True,
            stop_param="triggerPrice",
            client_id="uuid",
            quote="EUR",
            ping="https://api.bitvavo.com/v2/time",
        ),
        ExchangeProfile(
            "bithumb",
            "bithumb",
            "Bithumb",
            sandbox=False,
            exchange_stop=False,
            quote="KRW",
            ping="https://api.bithumb.com/public/ticker/BTC_KRW",
            note="KRW piyasası; Kore'de yerleşik hesap gerekir. Borsada stop yok.",
        ),
        ExchangeProfile(
            "whitebit",
            "whitebit",
            "WhiteBIT",
            sandbox=False,
            exchange_stop=True,
            stop_param="triggerPrice",
            ping="https://whitebit.com/api/v4/public/time",
        ),
        ExchangeProfile(
            "poloniex",
            "poloniex",
            "Poloniex",
            sandbox=True,
            exchange_stop=True,
            stop_param="triggerPrice",
            ping="https://api.poloniex.com/timestamp",
            ping_test="https://sand-spot-api-gateway.poloniex.com/timestamp",
        ),
        ExchangeProfile(
            "btcturk",
            "btcturk",
            "BTCTurk",
            sandbox=False,
            exchange_stop=False,
            websocket=False,
            quote="TRY",
            ping="https://api.btcturk.com/api/v2/server/exchangeinfo",
            note="TRY pariteleri; arbitrajda USDT/TRY köprüsü. Borsada stop emri yok.",
        ),
    )
}


def profile(name: str) -> ExchangeProfile:
    try:
        return PROFILES[name]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise KeyError(f"Desteklenmeyen borsa: {name} (desteklenen: {known})") from None
