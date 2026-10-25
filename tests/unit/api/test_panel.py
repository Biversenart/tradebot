from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from bot.api.app import CSRF_COOKIE, SESSION_COOKIE, create_app
from bot.control import BotControl

TOKEN = "s3cret-token-0123456789"


def client(control: BotControl) -> httpx.AsyncClient:
    app = create_app(control, SecretStr(TOKEN))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel")


def bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_short_token_refused(control: BotControl) -> None:
    with pytest.raises(ValueError):
        create_app(control, SecretStr("short"))


async def test_unauthenticated_is_rejected(control: BotControl) -> None:
    c = client(control)
    assert (await c.get("/health")).json() == {"status": "ok"}
    assert (await c.get("/api/status")).status_code == 401
    assert (await c.post("/api/kill")).status_code == 401
    assert (
        await c.get("/api/status", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    r = await c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_bearer_api(control: BotControl) -> None:
    c = client(control)
    st = (await c.get("/api/status", headers=bearer())).json()
    assert st["mode"] == "paper" and st["open_positions"] == 1 and st["equity"] == "10050"
    assert st["strategies"] == {"breakout:BTC/USDT": True}
    pos = (await c.get("/api/positions", headers=bearer())).json()
    assert pos[0]["symbol"] == "BTC/USDT" and pos[0]["targets_hit"] == 1
    assert (await c.get("/api/equity", headers=bearer())).json()[0]["equity"] == "10050"
    r = await c.get("/api/status", headers=bearer())
    assert (
        r.headers["x-frame-options"] == "DENY"
        and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    )


async def test_kill_switch_button_flow(control: BotControl) -> None:
    c = client(control)
    msg = (await c.post("/api/kill", headers=bearer())).json()["message"]
    assert "durduruldu" in msg
    assert (await c.get("/api/status", headers=bearer())).json()["kill_switch_active"] is True
    assert "manual" in control.kill_switch.state.reasons
    assert "devam" in (await c.post("/api/resume", headers=bearer())).json()["message"].lower()
    assert control.kill_switch.allows_new_orders()


async def test_browser_login_session_and_csrf(control: BotControl) -> None:
    c = client(control)
    assert (await c.post("/login", data={"token": "nope"})).status_code == 401
    r = await c.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303
    assert SESSION_COOKIE in c.cookies and CSRF_COOKIE in c.cookies
    page = await c.get("/")
    assert page.status_code == 200 and "KILL SWITCH" in page.text
    assert (await c.get("/api/status")).status_code == 200  # GET with session cookie
    assert (await c.post("/api/kill")).status_code == 403  # missing CSRF header
    assert (await c.post("/api/kill", headers={"X-CSRF-Token": "forged"})).status_code == 403
    ok = await c.post("/api/kill", headers={"X-CSRF-Token": c.cookies[CSRF_COOKIE]})
    assert ok.status_code == 200 and not control.kill_switch.allows_new_orders()
    await c.get("/logout")
    assert (await c.get("/api/status")).status_code == 401


async def test_login_rate_limit(control: BotControl) -> None:
    c = client(control)
    for _ in range(10):
        await c.post("/login", data={"token": "bad"})
    assert (await c.post("/login", data={"token": TOKEN})).status_code == 429


async def test_strategy_toggle(control: BotControl) -> None:
    c = client(control)
    r = await c.post("/api/strategies/breakout:BTC%2FUSDT?enabled=false", headers=bearer())
    assert r.status_code == 200 and control.disabled == {"breakout:BTC/USDT"}
    assert (await c.post("/api/strategies/nope?enabled=false", headers=bearer())).status_code == 404


async def test_reports_listing_and_safe_serving(control: BotControl) -> None:
    c = client(control)
    await c.post("/login", data={"token": TOKEN})
    listing = (await c.get("/reports")).text
    assert "BTC-USDT_1" in listing and "grafik" in listing
    md = await c.get("/reports/BTC-USDT_1.md")
    assert md.status_code == 200 and "&lt;script&gt;" in md.text  # escaped
    assert (await c.get("/reports/BTC-USDT_1.html")).text == "<html>chart</html>"
    for bad in ("..%2Fsecret.md", "%2E%2E%2Fsecret.md", "..secret.md", "missing.md"):
        assert (await c.get(f"/reports/{bad}")).status_code == 404


async def test_analyze_endpoint(control: BotControl) -> None:
    c = client(control)
    r = await c.post("/api/analyze?symbol=eth/usdt", headers=bearer())
    assert r.status_code == 200 and r.json()["symbol"] == "ETH/USDT"
    assert "Teknik Analiz" in r.json()["summary"]


async def test_ops_and_config_log_endpoints(control: BotControl) -> None:
    c = client(control)
    ops = (await c.get("/api/ops", headers=bearer())).json()
    assert ops["depeg"] == "normal" and "live" in ops["capital_cap"]
    await c.post("/api/strategies/breakout:BTC/USDT?enabled=false", headers=bearer())
    await c.post("/api/kill", headers=bearer())
    rows = (await c.get("/api/config-changes", headers=bearer())).json()
    paths = [r["path"] for r in rows]
    assert (
        "runtime.kill_switch" in paths and "runtime.strategies.breakout:BTC/USDT.enabled" in paths
    )
    assert all(r["source"] == "panel" for r in rows)
    assert rows[0]["operator"].startswith("Panel")


async def test_capital_cap_and_shadow_approve(control: BotControl) -> None:
    c = client(control)
    msg = (await c.post("/api/capital-cap?pct=50", headers=bearer())).json()["message"]
    assert msg.startswith("Reddedildi") and "Kademeli" in msg
    msg = (await c.post("/api/capital-cap?pct=20", headers=bearer())).json()["message"]
    assert "%20" in msg
    msg = (await c.post("/api/shadow/nope/approve", headers=bearer())).json()["message"]
    assert "bulunamadı" in msg
    rows = (await c.get("/api/config-changes", headers=bearer())).json()
    assert rows[0]["path"] == "operations.live_capital_cap_pct"
    assert (await c.post("/api/capital-cap?pct=20")).status_code == 401
