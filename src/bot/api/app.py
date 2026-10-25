"""Web panel (spec §5.10): token-protected FastAPI app serving a single-page UI.

Auth:
- API clients: `Authorization: Bearer <API_AUTH_TOKEN>` (no cookies -> no CSRF exposure).
- Browser: POST /login with the token -> random server-side session cookie (HttpOnly,
  SameSite=Strict) + a CSRF cookie; every state-changing request must echo the CSRF cookie in
  the `X-CSRF-Token` header (double submit). Failed logins are rate-limited.
The panel refuses to start without a token (fail closed). Bind to localhost; in Docker the
port is published on 127.0.0.1 only.

The UI (`static/`) is plain HTML/CSS/JS with no build step and no CDN: plotly.js is served
from the installed Python package. CSP allows scripts from 'self' only (no inline scripts).
Generated report pages (analysis / backtest HTML) are served sandboxed with their own CSP.
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import html
import io
import json
import re
import secrets
import time
from collections import deque
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from bot.api.jobs import JobError, JobRunner
from bot.api.views import PanelData
from bot.control import BotControl
from bot.strategies.registry import STRATEGIES

SESSION_COOKIE = "tb_session"
CSRF_COOKIE = "tb_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
STATIC_DIR = Path(__file__).parent / "static"
APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; frame-src 'self'; "
    "frame-ancestors 'self'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
# generated report pages: plotly inline bootstrap needs inline scripts -> sandboxed, no
# same-origin (cannot read cookies or call the API)
REPORT_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; frame-ancestors 'self'; "
    "sandbox allow-scripts"
)
CDN_PLOTLY = re.compile(r'<script[^>]+src="https://cdn\.plot\.ly/[^"]+"[^>]*></script>')


class PanelAuth:
    def __init__(self, token: SecretStr, max_failures: int = 10, window_s: float = 60.0) -> None:
        raw = token.get_secret_value()
        if len(raw) < 16:
            raise ValueError("API_AUTH_TOKEN en az 16 karakter olmalı.")
        self._token = raw.encode()
        self.sessions: dict[str, str] = {}  # session id -> csrf token
        self._failures: deque[float] = deque()
        self.max_failures = max_failures
        self.window_s = window_s

    def check_token(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._token)

    def locked_out(self) -> bool:
        now = time.monotonic()
        while self._failures and now - self._failures[0] > self.window_s:
            self._failures.popleft()
        return len(self._failures) >= self.max_failures

    def record_failure(self) -> None:
        self._failures.append(time.monotonic())

    def new_session(self) -> tuple[str, str]:
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[sid] = csrf
        return sid, csrf

    def authorize(self, request: Request) -> str:
        """Return 'bearer' or 'session'; raise 401/403 otherwise."""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            if self.check_token(header[7:].strip()):
                return "bearer"
            raise HTTPException(401, "Geçersiz token")
        sid = request.cookies.get(SESSION_COOKIE)
        if sid and sid in self.sessions:
            if request.method not in SAFE_METHODS:
                sent = request.headers.get("x-csrf-token", "")
                if not hmac.compare_digest(sent.encode(), self.sessions[sid].encode()):
                    raise HTTPException(403, "CSRF doğrulaması başarısız")
            return "session"
        raise HTTPException(401, "Giriş gerekli")


LOGIN_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bot paneli — giriş</title>
<link rel="icon" href="data:,"><link rel="stylesheet" href="/static/app.css"></head><body class="login">
<form method="post" action="/login" class="login-card"><h1>Kripto bot paneli</h1>
<p class="muted">.env içindeki API_AUTH_TOKEN ile giriş yapın.</p>
<input type="password" name="token" placeholder="API_AUTH_TOKEN" autocomplete="current-password" required autofocus>
<button type="submit" class="btn primary">Giriş</button>{error}</form></body></html>"""


@lru_cache(maxsize=1)
def plotly_js() -> str:
    from plotly.offline import get_plotlyjs

    return str(get_plotlyjs())


def report_response(path: Path) -> Response:
    """Generated report HTML: local plotly instead of the CDN, sandboxed CSP."""
    text = CDN_PLOTLY.sub('<script src="/vendor/plotly.min.js"></script>', path.read_text("utf-8"))
    resp = HTMLResponse(text)
    resp.headers["Content-Security-Policy"] = REPORT_CSP
    return resp


def markdown_response(path: Path, back: str) -> Response:
    text = html.escape(path.read_text(encoding="utf-8"))
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/app.css">'
        f'<body class="doc"><p><a href="{back}" target="_top">← Panele dön</a></p>'
        f'<pre class="report">{text}</pre></body>'
    )


class BacktestRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=10)
    strategies: list[str] = Field(default_factory=list, max_length=10)
    timeframe: str = "1h"
    since: str | None = None
    until: str | None = None


class DownloadRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=10)
    timeframes: list[str] = Field(default_factory=lambda: ["1h"], max_length=6)
    since: str
    until: str | None = None


def _require(request: Request) -> str:
    panel_auth: PanelAuth = request.app.state.auth
    return panel_auth.authorize(request)


Auth = Annotated[str, Depends(_require)]


def create_app(control: BotControl, token: SecretStr, panel: PanelData | None = None) -> FastAPI:
    auth = PanelAuth(token)
    data = panel or PanelData(control)
    app = FastAPI(title="Kripto bot paneli", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.state.panel = data
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        resp: Response = await call_next(request)
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        static = request.url.path.startswith(("/static/", "/vendor/"))
        resp.headers["Cache-Control"] = "private, max-age=300" if static else "no-store"
        resp.headers.setdefault("Content-Security-Policy", APP_CSP)
        return resp

    def page_or_login(request: Request) -> Response | None:
        try:
            auth.authorize(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return None

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/vendor/plotly.min.js")
    async def vendor_plotly() -> Response:
        return Response(plotly_js(), media_type="application/javascript")

    @app.get("/login", response_class=HTMLResponse)
    async def login_form() -> str:
        return LOGIN_HTML.replace("{error}", "")

    @app.post("/login")
    async def login(token: Annotated[str, Form()]) -> Response:
        if auth.locked_out():
            raise HTTPException(429, "Çok fazla hatalı deneme; biraz bekleyin.")
        if not auth.check_token(token):
            auth.record_failure()
            return HTMLResponse(
                LOGIN_HTML.replace("{error}", '<p class="error">Hatalı token</p>'),
                status_code=401,
            )
        sid, csrf = auth.new_session()
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="strict", secure=False)
        resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict", secure=False)
        return resp

    @app.get("/logout")
    async def logout(request: Request) -> Response:
        auth.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        resp.delete_cookie(CSRF_COOKIE)
        return resp

    @app.get("/")
    async def index(request: Request) -> Response:
        return page_or_login(request) or FileResponse(STATIC_DIR / "index.html")

    # ---------------------------------------------------------------- read-only API
    @app.get("/api/status")
    async def status(_: Auth) -> dict[str, Any]:
        return (await control.status()).to_dict()

    @app.get("/api/overview")
    async def overview(_: Auth) -> dict[str, Any]:
        return await data.overview()

    @app.get("/api/positions")
    async def positions(_: Auth) -> list[dict[str, Any]]:
        return await data.positions()

    @app.get("/api/trades")
    async def trades(
        _: Auth, limit: int = 200, symbol: str | None = None, strategy: str | None = None
    ) -> list[dict[str, Any]]:
        return await data.trades(limit, symbol or None, strategy or None)

    @app.get("/api/trades.csv")
    async def trades_csv(
        _: Auth, symbol: str | None = None, strategy: str | None = None
    ) -> Response:
        rows = await data.trades(2000, symbol or None, strategy or None)
        buf = io.StringIO()
        cols = [
            "closed_at",
            "opened_at",
            "symbol",
            "side",
            "strategy",
            "entry",
            "amount",
            "pnl",
            "fees",
            "r",
            "reason",
            "exchange",
            "id",
        ]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return Response(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="islemler.csv"'},
        )

    @app.get("/api/equity")
    async def equity(_: Auth) -> list[dict[str, str]]:
        return await control.equity_curve()

    @app.get("/api/performance")
    async def performance(_: Auth) -> dict[str, Any]:
        return await data.performance()

    @app.get("/api/strategies")
    async def strategies(_: Auth) -> list[dict[str, Any]]:
        return await data.strategies()

    @app.get("/api/symbols")
    async def symbols(_: Auth) -> dict[str, Any]:
        return {
            "symbols": data.symbols(),
            "timeframes": list(control.settings.config.marketdata.candle_timeframes),
            "exchanges": [n for n, c in control.settings.config.exchanges.items() if c.enabled],
            "strategies": sorted(STRATEGIES),
        }

    @app.get("/api/candles")
    async def candles(
        _: Auth, symbol: str, tf: str = "1h", exchange: str | None = None, limit: int = 500
    ) -> dict[str, Any]:
        return await data.candles(symbol.upper(), tf, exchange or None, limit)

    @app.get("/api/risk")
    async def risk(_: Auth) -> dict[str, Any]:
        return await data.risk()

    @app.get("/api/arbitrage")
    async def arbitrage(_: Auth, limit: int = 200) -> list[dict[str, Any]]:
        return await data.arbitrage(limit)

    @app.get("/api/config")
    async def config(_: Auth) -> dict[str, Any]:
        return data.config()

    @app.get("/api/exchanges")
    async def exchanges(_: Auth) -> list[dict[str, Any]]:
        return data.exchanges()

    @app.get("/api/events")
    async def events(_: Auth, limit: int = 50) -> list[dict[str, Any]]:
        return await control.events(min(limit, 500))

    @app.get("/api/ops")
    async def ops(_: Auth) -> dict[str, Any]:
        return control.ops_status()

    @app.get("/api/config-changes")
    async def config_changes(_: Auth, limit: int = 50) -> list[dict[str, Any]]:
        return await control.config_changes(min(limit, 500))

    @app.get("/api/stream")
    async def stream(request: Request, _: Auth, max_events: int = 0) -> Response:
        try:
            q = data.subscribe()
        except RuntimeError as exc:
            raise HTTPException(429, str(exc)) from exc

        async def gen() -> Any:
            try:
                yield "retry: 5000\n\n"
                sent = 0
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                    except TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    sent += 1
                    if max_events and sent >= max_events:
                        return
            finally:
                data.unsubscribe(q)

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    # ---------------------------------------------------------------- actions
    @app.post("/api/capital-cap")
    async def capital_cap(pct: Decimal, how: Auth) -> dict[str, str]:
        return {"message": await control.raise_capital_cap(pct, f"Panel ({how})")}

    @app.post("/api/shadow/{name}/approve")
    async def shadow_approve(name: str, how: Auth) -> dict[str, str]:
        return {"message": await control.approve_shadow(name, f"Panel ({how})")}

    @app.post("/api/kill")
    async def kill(how: Auth) -> dict[str, str]:
        return {"message": await control.stop(f"Panel ({how})")}

    @app.post("/api/resume")
    async def resume(how: Auth) -> dict[str, str]:
        return {"message": await control.resume(f"Panel ({how})")}

    @app.post("/api/strategies/{key:path}")
    async def toggle(key: str, enabled: bool, how: Auth) -> dict[str, Any]:
        if not await control.set_strategy_enabled(key, enabled, f"Panel ({how})"):
            raise HTTPException(404, "Strateji bulunamadı")
        return {"key": key, "enabled": enabled}

    @app.post("/api/analyze")
    async def analyze(symbol: str, _: Auth) -> dict[str, Any]:
        try:
            out = await control.analyze(symbol)
        except Exception as exc:
            raise HTTPException(400, f"Analiz yapılamadı: {exc}") from exc
        return {
            "symbol": out.symbol,
            "report": out.md_path.name,
            "chart": out.html_path.name,
            "summary": out.short_tr(14),
        }

    # ---------------------------------------------------------------- jobs (backtest / data)
    def jobs_or_404() -> JobRunner:
        if data.jobs is None:
            raise HTTPException(404, "İş yöneticisi bu çalıştırmada yok.")
        return data.jobs

    @app.get("/api/jobs")
    async def jobs(_: Auth) -> list[dict[str, Any]]:
        return jobs_or_404().list_jobs()

    @app.post("/api/jobs/backtest")
    async def job_backtest(body: BacktestRequest, _: Auth) -> dict[str, Any]:
        try:
            job = jobs_or_404().submit_backtest(
                body.symbols, body.strategies, body.timeframe, body.since, body.until
            )
        except JobError as exc:
            raise HTTPException(400, str(exc)) from exc
        return job.to_dict()

    @app.post("/api/jobs/download")
    async def job_download(body: DownloadRequest, _: Auth) -> dict[str, Any]:
        try:
            job = jobs_or_404().submit_download(
                body.symbols, body.timeframes, body.since, body.until
            )
        except JobError as exc:
            raise HTTPException(400, str(exc)) from exc
        return job.to_dict()

    @app.get("/api/datasets")
    async def datasets(_: Auth) -> list[dict[str, Any]]:
        return jobs_or_404().datasets()

    @app.get("/api/backtests")
    async def backtests(_: Auth) -> list[dict[str, Any]]:
        return jobs_or_404().runs()

    @app.get("/backtests/{run}/{name}")
    async def backtest_file(run: str, name: str, _: Auth) -> Response:
        path: Path | None = jobs_or_404().report_file(run, name)
        if path is None:
            raise HTTPException(404, "Rapor bulunamadı")
        if path.suffix == ".html":
            return report_response(path)
        return markdown_response(path, "/#/backtest")

    # ---------------------------------------------------------------- analysis reports
    @app.get("/api/reports")
    async def reports_api(_: Auth) -> list[dict[str, Any]]:
        out = []
        for md in control.reports()[:200]:
            chart = md.with_suffix(".html")
            out.append(
                {
                    "name": md.name,
                    "title": md.stem,
                    "chart": chart.name if chart.exists() else None,
                    "modified": md.stat().st_mtime,
                }
            )
        return out

    @app.get("/reports")
    async def reports(request: Request) -> Response:
        return page_or_login(request) or RedirectResponse("/#/analiz", status_code=303)

    @app.get("/reports/{name}")
    async def report_file(name: str, _: Auth) -> Response:
        path: Path | None = control.report_file(name)
        if path is None:
            raise HTTPException(404, "Rapor bulunamadı")
        if path.suffix == ".html":
            return report_response(path)
        return markdown_response(path, "/#/analiz")

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app
