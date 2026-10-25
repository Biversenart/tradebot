"""Comprehensive panel: read-only views, jobs, live stream, static UI and CSP."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from bot.api import insights
from bot.api.app import create_app
from bot.api.demo import build_demo
from bot.api.jobs import JobError, JobRunner
from bot.api.views import PanelData
from bot.config.schema import AppConfig
from bot.control import BotControl
from bot.core.event_bus import EventBus
from bot.core.events import AlertLevel, PositionEvent, RiskAlert
from bot.core.models import Order, OrderStatus, OrderType, Side
from bot.marketdata.history import parquet_path
from bot.marketdata.synthetic import generate_ohlcv
from bot.storage.models import EquityPointRow, PositionRow
from tests.unit.api.conftest import T
from tests.unit.api.conftest import control as control

TOKEN = "s3cret-token-0123456789"
H = {"Authorization": f"Bearer {TOKEN}"}


def closed(pid: str, strategy: str, pnl: str, hours: int) -> PositionRow:
    return PositionRow(
        position_id=pid,
        exchange="paper",
        symbol="BTC/USDT",
        side="long",
        strategy=strategy,
        amount=D(0),
        initial_amount=D(1),
        entry_price=D(100),
        initial_stop=D(95),
        stop_loss=D(95),
        take_profits=["110"],
        realized_pnl=D(pnl),
        status="closed",
        close_reason="tp1" if D(pnl) > 0 else "stop",
        opened_at=T + timedelta(hours=hours),
        closed_at=T + timedelta(hours=hours + 5),
    )


@pytest.fixture
async def world(
    control: BotControl, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, PanelData]]:
    repo = control.repo
    for i, (s, pnl) in enumerate([("breakout", "10"), ("breakout", "-5"), ("ema", "7.5")]):
        await repo.save_position(closed(f"c{i}", s, pnl, i * 24))
    await repo.save_order(
        Order(
            client_order_id="tbc0exit",
            exchange="paper",
            symbol="BTC/USDT",
            side=Side.SELL,
            order_type=OrderType.MARKET,
            amount=D(1),
            status=OrderStatus.FILLED,
            filled=D(1),
            average_price=D(110),
            created_at=T,
            updated_at=T + timedelta(hours=5),
        ),
        "exit",
        "c0",
        "breakout",
    )
    await repo.save_arbitrage("cross", "a→b BTC/USDT", D("0.4"), D(500), True, {})
    data = tmp_path / "data"
    df = generate_ohlcv(T, T + timedelta(days=30), "1h", 100.0, 3)
    path = parquet_path(data, "paper", "BTC/USDT", "1h")
    path.parent.mkdir(parents=True)
    df.to_parquet(path)

    async def download(
        ex: str, syms: list[str], tfs: list[str], since: object, until: object
    ) -> list[str]:
        return [f"{s} {t}: 5 yeni mum" for s in syms for t in tfs]

    jobs = JobRunner(control.settings.config, data, tmp_path / "bt", download, exchange="paper")
    bus = EventBus()
    panel = PanelData(
        control,
        jobs=jobs,
        bus=bus,
        data_root=data,
        default_exchange="paper",
        mark=lambda _e, _s: D(104),
    )
    app = create_app(control, SecretStr(TOKEN), panel)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        yield c, panel


async def get(c: httpx.AsyncClient, path: str) -> object:
    r = await c.get(path, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ insights (pure)
def test_stats_drawdown_and_markers() -> None:
    rows = [closed("a", "x", "10", 0), closed("b", "x", "-5", 1), closed("c", "y", "5", 2)]
    by = insights.strategy_stats(rows)
    assert by["x"] == {
        "trades": 2,
        "win_rate": 50.0,
        "pnl": "5",
        "profit_factor": "2.00",
        "expectancy_r": "0.50",
    }
    assert insights.total_stats(rows)["trades"] == 3
    assert insights.daily_pnl(rows)[0]["day"] == "2026-01-01"
    pts = [
        EquityPointRow(equity=D(v), reserve=D(0), timestamp=T + timedelta(hours=i))
        for i, v in enumerate([100, 120, 90, 130])
    ]
    series, worst = insights.drawdown(pts)
    assert worst == D(25) and series[2]["dd"] == "25.000"
    view = insights.position_view(closed("o", "x", "0", 0), D(104))
    assert view["unrealized_pnl"] == "0" and view["mark_live"] is True  # amount 0 (closed row)


# ------------------------------------------------------------------ read-only API
async def test_overview_positions_performance(world: tuple[httpx.AsyncClient, PanelData]) -> None:
    c, _ = world
    o = await get(c, "/api/overview")
    assert isinstance(o, dict)
    assert o["totals"]["trades"] == 3 and o["totals"]["pnl"] == "12.5"
    assert o["unrealized_pnl"] == "2.0" and o["exposure"] == "52.0"  # 0.5 @ 104 (entry 100)
    pos = await get(c, "/api/positions")
    assert isinstance(pos, list) and pos[0]["r"] == "0.40" and pos[0]["stop_on_exchange"] is False
    perf = await get(c, "/api/performance")
    assert isinstance(perf, dict) and set(perf["by_strategy"]) == {"breakout", "ema"}
    trades = await get(c, "/api/trades?strategy=ema")
    assert isinstance(trades, list) and [t["id"] for t in trades] == ["c2"]
    csv = await c.get("/api/trades.csv?strategy=breakout", headers=H)
    assert csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.strip().splitlines()
    assert lines[0].startswith("closed_at,") and len(lines) == 3


async def test_candles_with_markers_and_fallbacks(
    world: tuple[httpx.AsyncClient, PanelData],
) -> None:
    c, _ = world
    d = await get(c, "/api/candles?symbol=btc/usdt&tf=1h&limit=50")
    assert isinstance(d, dict) and len(d["bars"]) == 50 and d["source"].startswith("geçmiş")
    kinds = sorted(m["kind"] for m in d["markers"])
    assert kinds.count("entry") == 4 and kinds.count("exit") == 1  # 3 closed + 1 open entries
    assert d["open"][0]["stop"] == "100"
    none = await get(c, "/api/candles?symbol=XRP/USDT&tf=1h")
    assert isinstance(none, dict) and none["bars"] == [] and none["source"] == "veri yok"


async def test_risk_strategies_arbitrage_config(world: tuple[httpx.AsyncClient, PanelData]) -> None:
    c, _ = world
    r = await get(c, "/api/risk")
    assert isinstance(r, dict)
    assert [lim["name"] for lim in r["limits"]] == ["Günlük zarar", "Drawdown", "Toplam maruziyet"]
    assert r["errors"]["max"] == 5 and r["exposure"][0]["symbol"] == "BTC/USDT"
    s = await get(c, "/api/strategies")
    assert (
        isinstance(s, list) and s[0]["key"] == "breakout:BTC/USDT" and s[0]["stats"]["trades"] == 2
    )
    a = await get(c, "/api/arbitrage")
    assert isinstance(a, list) and a[0]["executed"] is True and a[0]["net_pct"] == "0.400"
    cfg = await get(c, "/api/config")
    assert isinstance(cfg, dict) and cfg["sends_real_orders"] is False
    text = json.dumps(cfg).lower()
    for secret in ("api_key", "secret", "telegram_bot_token", "api_auth_token", "password"):
        assert secret not in text
    meta = await get(c, "/api/symbols")
    assert isinstance(meta, dict) and "ema_crossover" in meta["strategies"]


async def test_read_endpoints_need_auth(world: tuple[httpx.AsyncClient, PanelData]) -> None:
    c, _ = world
    for path in (
        "/api/overview",
        "/api/risk",
        "/api/config",
        "/api/candles?symbol=BTC/USDT",
        "/api/jobs",
        "/api/stream",
        "/api/trades.csv",
        "/backtests/x/OZET.md",
    ):
        assert (await c.get(path)).status_code == 401, path
    r = await c.post("/api/jobs/backtest", json={"symbols": ["BTC/USDT"]})
    assert r.status_code == 401


# ------------------------------------------------------------------ jobs
async def wait_job(c: httpx.AsyncClient) -> dict[str, object]:
    for _ in range(300):
        jobs = await get(c, "/api/jobs")
        assert isinstance(jobs, list)
        if jobs and jobs[0]["status"] in ("done", "failed"):
            return dict(jobs[0])
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def test_download_job_and_validation(world: tuple[httpx.AsyncClient, PanelData]) -> None:
    c, _ = world
    bad = await c.post(
        "/api/jobs/download", headers=H, json={"symbols": ["../etc"], "since": "2024-01-01"}
    )
    assert bad.status_code == 400 and "Geçersiz sembol" in bad.json()["detail"]
    bad = await c.post("/api/jobs/download", headers=H, json={"symbols": ["BTC"], "since": "dün"})
    assert bad.status_code == 400
    ok = await c.post(
        "/api/jobs/download",
        headers=H,
        json={"symbols": ["btc", "eth/usdt"], "timeframes": ["1h", "4h"], "since": "2024-01-01"},
    )
    assert ok.status_code == 200 and ok.json()["params"]["symbols"] == ["BTC/USDT", "ETH/USDT"]
    job = await wait_job(c)
    assert job["status"] == "done" and len(job["result"]["lines"]) == 4  # type: ignore[index]
    ds = await get(c, "/api/datasets")
    assert isinstance(ds, list) and ds[0]["symbol"] == "BTC/USDT" and ds[0]["bars"] == 720


async def test_backtest_job_reports_and_safe_files(
    world: tuple[httpx.AsyncClient, PanelData],
) -> None:
    c, _ = world
    r = await c.post(
        "/api/jobs/backtest", headers=H, json={"symbols": ["BTC/USDT"], "strategies": ["nope"]}
    )
    assert r.status_code == 400 and "Bilinmeyen strateji" in r.json()["detail"]
    r = await c.post(
        "/api/jobs/backtest", headers=H, json={"symbols": ["BTC/USDT"], "strategies": ["dca"]}
    )
    assert r.status_code == 200
    busy = await c.post("/api/jobs/backtest", headers=H, json={"symbols": ["BTC/USDT"]})
    assert busy.status_code == 400 and "Başka bir iş" in busy.json()["detail"]
    job = await wait_job(c)
    assert job["status"] == "done", job
    run = job["result"]["run"]  # type: ignore[index]
    runs = await get(c, "/api/backtests")
    assert isinstance(runs, list) and runs[0]["run"] == run and "OZET.md" in runs[0]["files"]
    md = await c.get(f"/backtests/{run}/OZET.md", headers=H)
    assert md.status_code == 200 and "Backtest" in md.text
    html = await c.get(f"/backtests/{run}/BTC-USDT_dca.html", headers=H)
    assert html.status_code == 200 and "cdn.plot.ly" not in html.text
    assert "/vendor/plotly.min.js" in html.text
    assert "sandbox allow-scripts" in html.headers["content-security-policy"]
    for bad in ("..%2F..%2Fsecret", f"{run}%2F..", ".hidden"):
        assert (await c.get(f"/backtests/{bad}/OZET.md", headers=H)).status_code == 404
    assert (await c.get(f"/backtests/{run}/missing.md", headers=H)).status_code == 404


async def test_failed_backtest_reports_missing_data(
    world: tuple[httpx.AsyncClient, PanelData],
) -> None:
    c, _ = world
    await c.post("/api/jobs/backtest", headers=H, json={"symbols": ["XRP/USDT"]})
    job = await wait_job(c)
    assert job["status"] == "failed" and "verisi yok" in str(job["message"])


def test_job_runner_without_download(tmp_path: Path) -> None:
    jr = JobRunner(AppConfig(), tmp_path, tmp_path)
    with pytest.raises(JobError, match="kullanılamıyor"):
        jr.submit_download(["BTC/USDT"], ["1h"], "2024-01-01")
    assert jr.datasets() == [] and jr.runs() == []


# ------------------------------------------------------------------ live stream
async def test_stream_pushes_alerts_and_position_events(
    world: tuple[httpx.AsyncClient, PanelData],
) -> None:
    c, panel = world

    async def emit() -> None:
        for _ in range(100):
            if panel._subscribers:
                break
            await asyncio.sleep(0.01)
        await panel._on_alert(RiskAlert(level=AlertLevel.CRITICAL, code="x", message="dikkat"))
        await panel._on_position(
            PositionEvent(
                kind="closed",
                position_id="p",
                exchange="e",
                symbol="BTC/USDT",
                side="long",
                amount=D(1),
                realized_pnl=D(-3),
                reason="stop",
            )
        )

    task = asyncio.create_task(emit())
    r = await c.get("/api/stream?max_events=2", headers=H)
    await task
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["alert", "position"]
    assert events[0]["message"] == "dikkat" and events[1]["pnl"] == "-3"
    assert panel._subscribers == set()  # unsubscribed after the stream ended


# ------------------------------------------------------------------ static UI + CSP
async def test_static_ui_and_strict_csp(world: tuple[httpx.AsyncClient, PanelData]) -> None:
    c, _ = world
    assert (await c.get("/", follow_redirects=False)).status_code == 303
    await c.post("/login", data={"token": TOKEN})
    page = await c.get("/")
    assert page.status_code == 200 and "KILL SWITCH" in page.text
    assert "<script>" not in page.text and "onclick" not in page.text  # no inline JS
    csp = page.headers["content-security-policy"]
    assert "script-src 'self';" in csp and "https:" not in csp
    for asset in ("/static/app.js", "/static/pages.js", "/static/app.css"):
        r = await c.get(asset)
        assert r.status_code == 200 and "max-age" in r.headers["cache-control"], asset
    js = await c.get("/vendor/plotly.min.js")
    assert js.status_code == 200 and len(js.text) > 1_000_000
    assert js.headers["content-type"].startswith("application/javascript")
    api = await c.get("/api/status")
    assert api.headers["cache-control"] == "no-store"


def test_ui_has_no_external_urls() -> None:
    static = Path(__file__).parents[3] / "src" / "bot" / "api" / "static"
    for f in static.iterdir():
        text = f.read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, f.name
        assert "innerHTML = " not in text or "esc(" in text, f.name


# ------------------------------------------------------------------ demo
async def test_demo_builds_with_consistent_data(tmp_path: Path) -> None:
    control, panel, base = await build_demo(SecretStr(TOKEN), tmp_path)
    try:
        o = await panel.overview()
        assert o["totals"]["trades"] == 48 and o["open_positions"] == 2
        r = await panel.risk()
        assert not r["kill_switch"]["active"]
        assert float(r["limits"][1]["value_pct"]) < 10  # drawdown below the limit
        chart = await panel.candles("BTC/USDT")
        assert chart["bars"] and chart["markers"]
        assert (base / "data").exists()
    finally:
        await control.repo.close()


async def test_exchanges_listing_never_exposes_keys(
    world: tuple[httpx.AsyncClient, PanelData],
) -> None:
    c, _ = world
    rows = await get(c, "/api/exchanges")
    assert isinstance(rows, list) and len(rows) == 21
    by = {r["name"]: r for r in rows}
    assert by["upbit"]["exchange_stop"] is False and by["okx"]["needs_password"] is True
    assert by["binance"]["futures"] is True and by["kraken"]["quote"] == "USD"
    assert set(by["okx"]) >= {"keys_live", "keys_testnet"}
    assert all(isinstance(r["keys_live"], bool) for r in rows)  # yes/no only
