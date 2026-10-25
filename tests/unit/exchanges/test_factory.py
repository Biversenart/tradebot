"""Real ccxt client objects are built offline; check proxy, sandbox and key wiring."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal as D
from typing import Any

import pytest
from pydantic import SecretStr

from bot.config import EgressMode, Mode, Settings
from bot.config.schema import AppConfig
from bot.config.secrets import Secrets
from bot.core.models import OrderRequest, OrderType, Side
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.binance import BinanceAdapter
from bot.exchanges.btcturk import BtcturkAdapter
from bot.exchanges.ccxt_adapter import CcxtAdapter
from bot.exchanges.errors import NotSupportedError
from bot.exchanges.factory import build_exchanges
from bot.exchanges.paper import PaperExchange
from bot.net.egress import Egress

SOCKS = "socks5h://u:p@1.2.3.4:1080"


def egress(url: str | None = SOCKS) -> Egress:
    from bot.net.egress import parse_proxy_url

    return Egress(
        mode=EgressMode.PROXY if url else EgressMode.WIREGUARD,
        proxy=parse_proxy_url(SecretStr(url)) if url else None,
        expected_ip="8.8.8.8",
    )


def settings(mode: Mode, exchanges: dict[str, Any]) -> Settings:
    cfg = AppConfig.model_validate(
        {"mode": mode.value, "live_trading_confirmed": mode is Mode.LIVE, "exchanges": exchanges}
    )
    sec = Secrets(
        _env_file=None,
        binance_api_key=SecretStr("LIVEKEY"),
        binance_api_secret=SecretStr("LIVESECRET"),
        binance_testnet_api_key=SecretStr("TESTKEY"),
        binance_testnet_api_secret=SecretStr("TESTSECRET"),
        btcturk_api_key=SecretStr("BTKEY"),
        btcturk_api_secret=SecretStr("BTSECRET"),
    )
    return Settings(config=cfg, secrets=sec, mode=mode)


def client(a: ExchangeAdapter) -> Any:
    assert isinstance(a, CcxtAdapter)
    return a._client


@pytest.fixture
async def built() -> AsyncIterator[list[ExchangeAdapter]]:
    adapters: list[ExchangeAdapter] = []
    yield adapters
    for a in adapters:
        await a.close()


async def test_spot_testnet_uses_sandbox_and_proxy(built: list[ExchangeAdapter]) -> None:
    a = BinanceAdapter.create(
        market_type="spot",
        testnet=True,
        proxy_options=egress().ccxt_options(),
        api_key="k",
        secret="s",
    )
    built.append(a)
    c = client(a)
    assert "testnet.binance.vision" in c.urls["api"]["public"]
    assert c.socksProxy == SOCKS and c.wsSocksProxy == SOCKS
    assert c.aiohttp_trust_env is False
    assert c.enableRateLimit is True
    assert c.options["adjustForTimeDifference"] is True


async def test_futures_testnet_uses_demo_trading(built: list[ExchangeAdapter]) -> None:
    a = BinanceAdapter.create(
        market_type="futures", testnet=True, proxy_options={}, api_key="k", secret="s"
    )
    built.append(a)
    c = client(a)
    assert c.id == "binanceusdm"
    assert "demo-fapi.binance.com" in c.urls["api"]["fapiPublic"]


async def test_live_spot_uses_real_endpoint(built: list[ExchangeAdapter]) -> None:
    a = BinanceAdapter.create(market_type="spot", testnet=False, proxy_options={})
    built.append(a)
    assert "api.binance.com" in client(a).urls["api"]["public"]


def test_invalid_market_type() -> None:
    with pytest.raises(NotSupportedError):
        BinanceAdapter.create(market_type="options", testnet=True, proxy_options={})


async def test_paper_mode_wraps_public_data_without_keys(built: list[ExchangeAdapter]) -> None:
    out = build_exchanges(settings(Mode.PAPER, {"binance": {"enabled": True}}), egress())
    built.extend(out.values())
    paper = out["binance"]
    assert isinstance(paper, PaperExchange)
    src = paper._source
    assert src is not None
    c = client(src)
    assert not c.apiKey and not c.secret
    assert "api.binance.com" in c.urls["api"]["public"]  # live public data
    assert c.socksProxy == SOCKS
    assert (await paper.fetch_balance())["USDT"].free == D(10000)


async def test_testnet_mode_uses_testnet_keys_only(built: list[ExchangeAdapter]) -> None:
    out = build_exchanges(
        settings(
            Mode.TESTNET,
            {
                "binance": {"enabled": True, "testnet": True},
                "btcturk": {"enabled": True, "testnet": True},
            },
        ),
        egress(),
    )
    built.extend(out.values())
    b = client(out["binance"])
    assert (b.apiKey, b.secret) == ("TESTKEY", "TESTSECRET")
    bt = out["btcturk"]
    assert isinstance(bt, BtcturkAdapter)
    assert not client(bt).apiKey
    req = OrderRequest(
        client_order_id="x",
        exchange="btcturk",
        symbol="BTC/TRY",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        amount=D(1),
    )
    with pytest.raises(NotSupportedError):
        await bt.create_order(req)


async def test_live_mode_uses_live_keys(built: list[ExchangeAdapter]) -> None:
    out = build_exchanges(
        settings(Mode.LIVE, {"binance": {"enabled": True, "testnet": False}}), egress()
    )
    built.extend(out.values())
    assert client(out["binance"]).apiKey == "LIVEKEY"


def test_backtest_builds_nothing() -> None:
    assert build_exchanges(settings(Mode.BACKTEST, {"binance": {"enabled": True}}), egress()) == {}


def test_unknown_exchange() -> None:
    with pytest.raises(NotSupportedError):
        build_exchanges(settings(Mode.PAPER, {"kraken": {"enabled": True}}), egress())
