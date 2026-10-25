"""The 21 supported exchanges: profiles vs. installed ccxt, factory wiring, the generic
adapter (stop params, client ids, testnet guard) and the rule-9 guard. All offline."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import ccxt
import ccxt.pro as ccxt_pro
import pytest
from pydantic import SecretStr

from bot.config import Mode, Settings
from bot.config.credentials import load_exchange_keys
from bot.config.schema import AppConfig
from bot.config.secrets import Secrets
from bot.core.models import OrderRequest, OrderType, Side
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import NotSupportedError, OrderNotFoundError
from bot.exchanges.factory import SUPPORTED, build_exchanges, build_public_adapter
from bot.exchanges.generic import GenericCcxtAdapter, exchange_client_id
from bot.exchanges.paper import PaperExchange
from bot.exchanges.profiles import PROFILES, profile
from bot.net.exchange_access import ping_url
from tests.unit.exchanges.test_factory import SOCKS, egress

GENERIC = [p for p in PROFILES.values() if p.name not in ("binance", "btcturk")]
STOP_FLAGS = (
    "createStopMarketOrder",
    "createStopOrder",
    "createTriggerOrder",
    "createStopLossOrder",
)


@pytest.fixture
async def built() -> AsyncIterator[list[ExchangeAdapter]]:
    adapters: list[ExchangeAdapter] = []
    yield adapters
    for a in adapters:
        await a.close()


# ------------------------------------------------------------------ profiles vs ccxt
def test_twenty_one_exchanges_supported() -> None:
    assert len(PROFILES) == 21 and set(SUPPORTED) == set(PROFILES)


@pytest.mark.parametrize("p", list(PROFILES.values()), ids=lambda p: p.name)
def test_profile_matches_installed_ccxt(p: Any) -> None:
    assert hasattr(ccxt, p.ccxt_id), p.ccxt_id
    c = getattr(ccxt, p.ccxt_id)()
    assert c.has.get("spot") and c.has.get("fetchOHLCV")
    assert p.sandbox == bool(c.urls.get("test")), "sandbox"
    assert p.exchange_stop == any(c.has.get(k) for k in STOP_FLAGS), "exchange_stop"
    if p.stop_param == "stopLossPrice":
        assert not p.exchange_stop or c.has.get("createStopLossOrder")
    assert p.needs_password == bool(c.requiredCredentials.get("password")), "password"
    assert p.websocket == hasattr(ccxt_pro, p.ccxt_id), "websocket"
    assert p.ping.startswith("https://") and (not p.ping_test or p.ping_test.startswith("https://"))
    assert ping_url(p.name, "spot", False) == p.ping


def test_profile_lookup_error_lists_supported() -> None:
    with pytest.raises(KeyError, match="desteklenen: binance"):
        profile("nosuch")


# ------------------------------------------------------------------ factory wiring
@pytest.mark.parametrize("p", GENERIC, ids=lambda p: p.name)
async def test_public_adapter_goes_through_egress(p: Any, built: list[ExchangeAdapter]) -> None:
    cfg = AppConfig.model_validate({"exchanges": {p.name: {"enabled": True}}})
    a = build_public_adapter(p.name, cfg.exchanges[p.name], egress(), testnet=False)
    built.append(a)
    assert isinstance(a, GenericCcxtAdapter) and a.name == p.name
    c = a._client
    assert c.socksProxy == SOCKS and c.apiKey in (None, "")
    if p.websocket:
        assert c.wsSocksProxy == SOCKS
    assert c.aiohttp_trust_env is False  # never use HTTP(S)_PROXY from the environment
    assert a.supports_exchange_stop == p.exchange_stop


def settings(mode: Mode, name: str, keys: dict[tuple[str, bool], Any]) -> Settings:
    cfg = AppConfig.model_validate(
        {
            "mode": mode.value,
            "live_trading_confirmed": mode is Mode.LIVE,
            "exchanges": {name: {"enabled": True, "testnet": mode is Mode.TESTNET}},
        }
    )
    return Settings(cfg, Secrets(_env_file=None), mode, keys)


def keys_for(
    name: str, testnet: bool, password: bool, tmp_path: Path
) -> dict[tuple[str, bool], Any]:
    env = tmp_path / f"{name}.env"
    t = "TESTNET_" if testnet else ""
    env.write_text(
        f"{name.upper()}_{t}API_KEY=k-{t}\n{name.upper()}_{t}API_SECRET=s-{t}\n"
        + (f"{name.upper()}_{t}API_PASSWORD=pw\n" if password else ""),
        encoding="utf-8",
    )
    return load_exchange_keys([name], env)


async def test_live_keys_and_passphrase(tmp_path: Path, built: list[ExchangeAdapter]) -> None:
    out = build_exchanges(
        settings(Mode.LIVE, "okx", keys_for("okx", False, True, tmp_path)), egress()
    )
    built.extend(out.values())
    c = out["okx"]._client  # type: ignore[attr-defined]
    assert (c.apiKey, c.secret, c.password) == ("k-", "s-", "pw")
    with pytest.raises(NotSupportedError, match="OKX_API_PASSWORD"):
        build_exchanges(
            settings(Mode.LIVE, "okx", keys_for("okx", False, False, tmp_path)), egress()
        )


async def test_testnet_sandbox_vs_data_only(tmp_path: Path, built: list[ExchangeAdapter]) -> None:
    # Bybit has a testnet: testnet keys + sandbox URLs
    out = build_exchanges(
        settings(Mode.TESTNET, "bybit", keys_for("bybit", True, False, tmp_path)), egress()
    )
    built.extend(out.values())
    c = out["bybit"]._client  # type: ignore[attr-defined]
    assert c.apiKey == "k-TESTNET_" and "testnet" in str(c.urls["api"])
    # Kraken has none: no keys at all, orders refused (public data only)
    live_keys = keys_for("kraken", False, False, tmp_path) | keys_for(
        "kraken", True, False, tmp_path
    )
    out = build_exchanges(settings(Mode.TESTNET, "kraken", live_keys), egress())
    built.extend(out.values())
    kr = out["kraken"]
    assert kr._client.apiKey in (None, "")  # type: ignore[attr-defined]
    with pytest.raises(NotSupportedError, match="test ağı sunmuyor"):
        await kr.create_order(req(OrderType.MARKET, "kraken"))


async def test_paper_mirrors_stop_capability(built: list[ExchangeAdapter]) -> None:
    for name, expected in (("upbit", False), ("kucoin", True)):
        out = build_exchanges(settings(Mode.PAPER, name, {}), egress())
        built.extend(a._source for a in out.values() if isinstance(a, PaperExchange) and a._source)
        assert isinstance(out[name], PaperExchange)
        assert out[name].supports_exchange_stop is expected


async def test_futures_only_on_binance() -> None:
    cfg = AppConfig.model_validate(
        {"exchanges": {"bybit": {"enabled": True, "market_type": "futures"}}}
    )
    with pytest.raises(NotSupportedError, match="yalnızca spot"):
        build_public_adapter("bybit", cfg.exchanges["bybit"], egress(), testnet=False)


# ------------------------------------------------------------------ generic adapter
class FakeClient:
    def __init__(self) -> None:
        self.has = {"fetchClosedOrders": True}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.open: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []

    async def create_order(self, *a: Any) -> dict[str, Any]:
        self.calls.append(("create_order", a))
        params = a[-1]
        return {
            "id": "X1",
            "clientOrderId": params["clientOrderId"],
            "status": "open",
            "type": a[1],
            "filled": 0,
            "timestamp": 1_700_000_000_000,
        }

    async def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return self.open

    async def fetch_closed_orders(self, symbol: str) -> list[dict[str, Any]]:
        return self.closed

    async def close(self) -> None:
        pass


def req(kind: OrderType, exchange: str = "kraken", cid: str = "tb" + "a" * 30) -> OrderRequest:
    return OrderRequest(
        client_order_id=cid,
        exchange=exchange,
        symbol="BTC/USD",
        side=Side.SELL,
        order_type=kind,
        amount=D("0.01"),
        price=D(100) if kind is OrderType.LIMIT else None,
        stop_price=D(95) if kind in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT) else None,
    )


def test_client_id_formats() -> None:
    our = "tb" + "0123456789abcdef" * 2
    our = our[:32]
    assert exchange_client_id(our, "plain") == our
    gate = exchange_client_id(our, "gate")
    assert gate.startswith("t-") and len(gate) <= 30
    u = exchange_client_id(our, "uuid")
    assert uuid.UUID(u) and u == exchange_client_id(our, "uuid")  # deterministic -> idempotent
    n = int(exchange_client_id(our, "int"))
    assert 0 <= n < 2**40 and exchange_client_id(our, "int") == str(n)


@pytest.mark.parametrize("name", ["kraken", "okx", "gate", "bitfinex"])
async def test_stop_params_and_client_id_mapping(name: str) -> None:
    p = profile(name)
    fake = FakeClient()
    a = GenericCcxtAdapter(p, fake)
    order = await a.create_order(req(OrderType.STOP_MARKET, name))
    _, args = fake.calls[-1]
    params = args[-1]
    assert params[p.stop_param] == 95.0 and args[1] == "market"
    assert params["clientOrderId"] == exchange_client_id("tb" + "a" * 30, p.client_id)
    assert order.client_order_id == "tb" + "a" * 30  # mapped back to our id


async def test_stopless_exchange_refuses_stop_orders() -> None:
    a = GenericCcxtAdapter(profile("upbit"), FakeClient())
    assert a.supports_exchange_stop is False
    with pytest.raises(NotSupportedError, match="kural 9"):
        await a.create_order(req(OrderType.STOP_MARKET, "upbit"))
    await a.create_order(req(OrderType.MARKET, "upbit"))  # market orders are fine (exits)


async def test_fetch_order_by_client_id_searches_open_then_closed() -> None:
    fake = FakeClient()
    a = GenericCcxtAdapter(profile("bitvavo"), fake)
    cid = "tb" + "b" * 30
    ex_id = exchange_client_id(cid, "uuid")
    fake.closed = [
        {
            "id": "Z9",
            "clientOrderId": ex_id,
            "status": "closed",
            "side": "buy",
            "type": "market",
            "amount": 1,
            "filled": 1,
            "average": 10,
            "timestamp": int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000),
        }
    ]
    o = await a.fetch_order_by_client_id(cid, "BTC/EUR")
    assert o.exchange_order_id == "Z9" and o.client_order_id == cid
    fake.closed = []
    with pytest.raises(OrderNotFoundError):
        await a.fetch_order_by_client_id(cid, "BTC/EUR")


# ------------------------------------------------------------------ credentials + config
def test_credentials_env_overrides_file_and_masks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "KUCOIN_API_KEY=file\nKUCOIN_API_SECRET=fs\nKUCOIN_API_PASSWORD=\nGATE_API_KEY=only-key\n"
    )
    monkeypatch.setenv("KUCOIN_API_KEY", "from-env")
    keys = load_exchange_keys(["kucoin", "gate"], env)
    k = keys[("kucoin", False)]
    assert k.api_key.get_secret_value() == "from-env" and k.password is None
    assert ("gate", False) not in keys  # secret missing -> not configured
    assert "from-env" not in repr(k) and "fs" not in repr(k)


def test_per_exchange_symbols() -> None:
    cfg = AppConfig.model_validate(
        {
            "universe": {"symbols": ["BTC/USDT"]},
            "exchanges": {
                "kraken": {"enabled": True, "symbols": ["BTC/USD"]},
                "okx": {"enabled": True},
            },
        }
    )
    assert cfg.symbols_for("kraken") == ("BTC/USD",) and cfg.symbols_for("okx") == ("BTC/USDT",)


# ------------------------------------------------------------------ rule 9 end to end
async def test_no_positions_on_exchanges_without_stop_orders(tmp_path: Path) -> None:
    from bot.core.events import SignalEvent
    from bot.storage.repository import Repository
    from tests.unit.execution.test_coordinator import INFO, SYM, World, book, signal

    ex = PaperExchange("paper", initial_balances={"USDT": D(10_000)}, markets={SYM: INFO})
    await ex.process_order_book(book("99.9", "100"))
    ex.supports_exchange_stop = False  # e.g. paper over Upbit / BTCTurk
    repo = await Repository.connect(f"sqlite+aiosqlite:///{tmp_path / 'r9.db'}")
    try:
        w = World(ex, repo)
        await w.coord.on_signal(SignalEvent(signal=signal()))
        assert await repo.open_positions() == []
        assert ex._orders == {}  # nothing was even sent
        ex.supports_exchange_stop = True
        await w.coord.on_signal(SignalEvent(signal=signal()))
        assert len(await repo.open_positions()) == 1
    finally:
        await repo.close()


async def test_trading_stack_builds_no_strategies_on_stopless_exchange(tmp_path: Path) -> None:
    from bot.core.event_bus import EventBus
    from bot.trading import TradingStack

    cfg = AppConfig.model_validate(
        {"universe": {"symbols": ["BTC/USDT"]}, "strategies": {"breakout": {"enabled": True}}}
    )
    db = SecretStr(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    stopless = PaperExchange("btcturk")
    stopless.supports_exchange_stop = False
    ok = PaperExchange("okx")
    stack = await TradingStack.build(
        Settings(cfg, Secrets(_env_file=None, database_url=db), Mode.PAPER),
        EventBus(),
        {"btcturk": stopless, "okx": ok},
        {},
        lambda: True,
    )
    assert {s.exchange for s in stack.strategies} == {"okx"}
    await stack.close()


def test_feed_drops_unlisted_symbols() -> None:
    from bot.core.event_bus import EventBus
    from bot.marketdata.feed import MarketDataFeed
    from tests.fakes import FakeAdapter

    class Listed(FakeAdapter):
        def symbols(self) -> list[str]:
            return ["BTC/USD"]

    feed = MarketDataFeed(Listed("kraken"), EventBus(), ["BTC/USD", "BTC/USDT"], ["1h"])
    assert feed.restrict_to_listed() == ["BTC/USDT"]
    assert feed.symbols == ["BTC/USD"] and set(feed.builders) == {"BTC/USD"}
