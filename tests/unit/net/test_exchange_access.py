from __future__ import annotations

import socket
from collections.abc import AsyncIterator

import pytest
from aiohttp import web

from bot.config import EgressMode
from bot.net.egress import Egress
from bot.net.errors import ErrorKind
from bot.net.exchange_access import check_exchange_access, ping_url


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
async def base_url() -> AsyncIterator[str]:
    async def restricted(_: web.Request) -> web.Response:
        return web.Response(
            status=451, text='{"msg":"Service unavailable from a restricted location"}'
        )

    async def forbidden(_: web.Request) -> web.Response:
        return web.Response(status=403, text="restricted location")

    async def ok(_: web.Request) -> web.Response:
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/451", restricted)
    app.router.add_get("/403", forbidden)
    app.router.add_get("/ok", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


def direct() -> Egress:
    return Egress(mode=EgressMode.DIRECT_VPS, proxy=None, expected_ip=None, timeout_seconds=2)


@pytest.mark.parametrize("path", ["/451", "/403"])
async def test_restricted_location_detected(base_url: str, path: str) -> None:
    async with direct().new_http_session() as http:
        res = await check_exchange_access(http, "binance", base_url + path)
    assert not res.ok
    assert res.kind is ErrorKind.RESTRICTED_LOCATION
    assert "restricted location" in res.message


async def test_access_ok(base_url: str) -> None:
    async with direct().new_http_session() as http:
        res = await check_exchange_access(http, "binance", base_url + "/ok")
    assert res.ok and res.status == 200


async def test_unreachable_is_network() -> None:
    async with direct().new_http_session() as http:
        res = await check_exchange_access(http, "binance", f"http://127.0.0.1:{free_port()}/")
    assert res.kind is ErrorKind.NETWORK


def test_ping_urls() -> None:
    assert ping_url("binance", "spot", True) == "https://testnet.binance.vision/api/v3/ping"
    assert ping_url("binance", "futures", False) == "https://fapi.binance.com/fapi/v1/ping"
    with pytest.raises(KeyError):
        ping_url("kraken", "spot", False)
