"""Egress routing: traffic goes through the proxy; proxy loss never falls back to direct."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from aiohttp import web
from pydantic import SecretStr

from bot.config import EgressMode, Mode, Settings
from bot.config.schema import AppConfig
from bot.config.secrets import Secrets
from bot.net.egress import Egress, parse_proxy_url
from bot.net.errors import EgressMisconfiguredError, EgressUnavailableError


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class CountingServer:
    def __init__(self, body: str) -> None:
        self.body = body
        self.hits: list[str] = []
        self.port = 0

    async def handler(self, request: web.Request) -> web.Response:
        # A forward proxy receives the absolute target URL in the request line.
        self.hits.append(str(request.url))
        return web.Response(text=self.body)


ServerFactory = Callable[[str], Awaitable[CountingServer]]


@pytest.fixture
async def start_server() -> AsyncIterator[ServerFactory]:
    created: list[tuple[web.AppRunner, CountingServer]] = []

    async def start(body: str) -> CountingServer:
        srv = CountingServer(body)
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", srv.handler)
        runner = web.AppRunner(app)
        await runner.setup()
        srv.port = free_port()
        await web.TCPSite(runner, "127.0.0.1", srv.port).start()
        created.append((runner, srv))
        return srv

    yield start
    for runner, _ in created:
        await runner.cleanup()


def settings_for(mode: EgressMode, url: str | None, trading: Mode = Mode.PAPER) -> Settings:
    cfg = AppConfig.model_validate({"mode": trading.value, "egress": {"mode": mode.value}})
    sec = Secrets(
        _env_file=None,
        egress_proxy_url=SecretStr(url) if url else None,
        egress_expected_ip="8.8.8.8",
    )
    return Settings(config=cfg, secrets=sec, mode=trading)


# ---------------------------------------------------------------- parsing / options


@pytest.mark.parametrize(
    ("url", "scheme"),
    [
        ("socks5h://u:p@1.2.3.4:1080", "socks5h"),
        ("http://1.2.3.4:3128", "http"),
        ("SOCKS5://1.2.3.4:1080", "socks5"),
    ],
)
def test_parse_proxy_url(url: str, scheme: str) -> None:
    ep = parse_proxy_url(SecretStr(url))
    assert ep.scheme == scheme
    assert ep.host == "1.2.3.4"
    assert "p@" not in repr(ep)


@pytest.mark.parametrize(
    "url", ["ftp://1.2.3.4:21", "socks5://1.2.3.4", "1.2.3.4:1080", "socks5://:1080", "http://h:x"]
)
def test_parse_proxy_url_invalid(url: str) -> None:
    with pytest.raises(EgressMisconfiguredError):
        parse_proxy_url(SecretStr(url))


def test_ccxt_options_socks_covers_rest_and_ws() -> None:
    eg = Egress.from_settings(settings_for(EgressMode.PROXY, "socks5h://u:p@1.2.3.4:1080"))
    assert eg.ccxt_options() == {
        "socksProxy": "socks5h://u:p@1.2.3.4:1080",
        "wsSocksProxy": "socks5h://u:p@1.2.3.4:1080",
    }


def test_ccxt_options_http_covers_rest_and_ws() -> None:
    eg = Egress.from_settings(settings_for(EgressMode.MANAGED_PROXY, "http://1.2.3.4:3128"))
    assert eg.ccxt_options() == {
        "httpsProxy": "http://1.2.3.4:3128",
        "wssProxy": "http://1.2.3.4:3128",
    }


def test_tunnel_modes_inject_no_proxy() -> None:
    for mode in (EgressMode.WIREGUARD, EgressMode.DIRECT_VPS):
        eg = Egress.from_settings(settings_for(mode, "socks5://1.2.3.4:1080"))
        assert eg.proxy is None
        assert eg.ccxt_options() == {}


def test_proxy_mode_without_url_is_refused() -> None:
    with pytest.raises(EgressMisconfiguredError):
        Egress.from_settings(settings_for(EgressMode.PROXY, None))


# ---------------------------------------------------------------- real sockets


async def test_requests_go_through_http_proxy(start_server: ServerFactory) -> None:
    target = await start_server("direct-should-not-be-hit")
    proxy = await start_server("8.8.8.8")
    eg = Egress.from_settings(settings_for(EgressMode.PROXY, f"http://127.0.0.1:{proxy.port}"))
    async with eg.new_http_session() as http:
        body = await http.get_text(f"http://127.0.0.1:{target.port}/ip")
    assert body == "8.8.8.8"
    assert len(proxy.hits) == 1
    assert proxy.hits[0].endswith(f"127.0.0.1:{target.port}/ip")
    assert target.hits == []


@pytest.mark.parametrize("scheme", ["http", "socks5"])
async def test_proxy_down_never_falls_back_to_direct(
    start_server: ServerFactory, scheme: str
) -> None:
    target = await start_server("8.8.8.8")
    dead_port = free_port()  # nothing listens here: the "proxy" is down
    eg = Egress.from_settings(
        settings_for(EgressMode.PROXY, f"{scheme}://user:secretpw@127.0.0.1:{dead_port}")
    )
    async with eg.new_http_session() as http:
        for _ in range(3):
            with pytest.raises(EgressUnavailableError) as info:
                await http.get_text(f"http://127.0.0.1:{target.port}/ip")
            assert "secretpw" not in str(info.value)
    assert target.hits == []  # the target was never contacted directly


async def test_env_proxy_variables_are_ignored(
    start_server: ServerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = await start_server("8.8.8.8")
    dead_port = free_port()
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{dead_port}")
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{dead_port}")
    eg = Egress.from_settings(settings_for(EgressMode.WIREGUARD, None))
    async with eg.new_http_session() as http:
        assert await http.get_text(f"http://127.0.0.1:{target.port}/") == "8.8.8.8"


def test_no_direct_ccxt_import_in_net() -> None:
    src = Path(__file__).resolve().parents[3] / "src" / "bot" / "net"
    for f in src.glob("*.py"):
        assert "import ccxt" not in f.read_text(encoding="utf-8")
