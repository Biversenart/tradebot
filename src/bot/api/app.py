"""Web panel (spec §5.10): token-protected FastAPI app.

Auth:
- API clients: `Authorization: Bearer <API_AUTH_TOKEN>` (no cookies -> no CSRF exposure).
- Browser: POST /login with the token -> random server-side session cookie (HttpOnly,
  SameSite=Strict) + a CSRF cookie; every state-changing request must echo the CSRF cookie in
  the `X-CSRF-Token` header (double submit). Failed logins are rate-limited.
The panel refuses to start without a token (fail closed). Bind to localhost; in Docker the
port is published on 127.0.0.1 only.
"""

from __future__ import annotations

import hmac
import html
import secrets
import time
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import SecretStr

from bot.control import BotControl

SESSION_COOKIE = "tb_session"
CSRF_COOKIE = "tb_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


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
<style>body{font-family:system-ui;max-width:360px;margin:15vh auto;padding:16px}
input,button{width:100%;padding:10px;margin:6px 0;font-size:16px}</style></head><body>
<h2>Kripto bot paneli</h2><form method="post" action="/login">
<input type="password" name="token" placeholder="API_AUTH_TOKEN" autocomplete="current-password" required>
<button type="submit">Giriş</button></form>{error}</body></html>"""

DASHBOARD_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bot paneli</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
body{font-family:system-ui,sans-serif;margin:0;padding:16px;max-width:1200px;margin:auto;color:#222}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.card{border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #eee;padding:6px;text-align:left}
#kill{background:#c0392b;color:#fff;border:0;border-radius:12px;font-size:22px;font-weight:700;padding:18px 28px;cursor:pointer}
#resume{background:#27ae60;color:#fff;border:0;border-radius:8px;padding:10px 16px;cursor:pointer}
.bad{color:#c0392b;font-weight:600}.ok{color:#27ae60;font-weight:600}nav a{margin-right:12px}
</style></head><body>
<header><div><h1 style="margin:0">Kripto bot</h1><nav><a href="/">Panel</a><a href="/reports">Analiz raporları</a>
<a href="/logout">Çıkış</a></nav></div>
<div><button id="kill" title="Yeni emirleri durdur">⛔ KILL SWITCH</button> <button id="resume">Devam</button></div></header>
<div class="card" id="status">Yükleniyor…</div>
<div class="card"><h3>Equity</h3><div id="equity" style="height:300px"></div></div>
<div class="card"><h3>Açık pozisyonlar</h3><table id="positions"></table></div>
<div class="card"><h3>Son işlemler</h3><table id="trades"></table></div>
<div class="card"><h3>Stratejiler</h3><table id="strategies"></table></div>
<div class="card"><h3>Risk olayları</h3><table id="events"></table></div>
<div class="card"><h3>Operasyonel güvenlik</h3><pre id="ops" style="white-space:pre-wrap"></pre></div>
<div class="card"><h3>Config değişiklik günlüğü</h3><table id="changes"></table></div>
<script>
const csrf = document.cookie.split('; ').find(c=>c.startsWith('tb_csrf='))?.split('=')[1] || '';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, method='GET'){
  const r = await fetch(path, {method, headers: {'X-CSRF-Token': csrf}, credentials: 'same-origin'});
  if (r.status === 401) { location.href = '/login'; return null; }
  return r.json();
}
function table(el, rows, cols){
  el.innerHTML = '<tr>' + cols.map(c=>'<th>'+esc(c[1])+'</th>').join('') + '</tr>' +
    rows.map(r=>'<tr>' + cols.map(c=>'<td>'+esc(r[c[0]])+'</td>').join('') + '</tr>').join('');
}
async function refresh(){
  const s = await api('/api/status'); if(!s) return;
  document.getElementById('status').innerHTML =
    `<b>Mod:</b> ${esc(s.mode)} · <b>Bakiye:</b> ${esc(s.equity)} · <b>Rezerv:</b> ${esc(s.reserve)} · ` +
    `<b>Kill switch:</b> ${s.kill_switch_active ? '<span class=bad>AKTİF</span> '+esc(Object.values(s.kill_reasons).join('; ')) : '<span class=ok>kapalı</span>'} · ` +
    `<b>IP:</b> ${s.egress_ok ? '<span class=ok>doğrulandı</span>' : '<span class=bad>DOĞRULANMADI</span>'} · <b>Açık pozisyon:</b> ${s.open_positions}`;
  const st = document.getElementById('strategies');
  st.innerHTML = '<tr><th>Strateji</th><th>Durum</th><th></th></tr>' + Object.entries(s.strategies).map(([k,v]) =>
    `<tr><td>${esc(k)}</td><td>${v?'açık':'kapalı'}</td><td><button data-k="${esc(k)}" data-v="${!v}">${v?'Kapat':'Aç'}</button></td></tr>`).join('');
  st.querySelectorAll('button').forEach(b => b.onclick = async () => { await api(`/api/strategies/${encodeURIComponent(b.dataset.k)}?enabled=${b.dataset.v}`, 'POST'); refresh(); });
  table(document.getElementById('positions'), await api('/api/positions'), [['symbol','Sembol'],['side','Yön'],['amount','Miktar'],['entry','Giriş'],['stop','Stop'],['targets_hit','TP'],['strategy','Strateji']]);
  table(document.getElementById('trades'), await api('/api/trades'), [['closed_at','Kapanış'],['symbol','Sembol'],['side','Yön'],['pnl','PnL'],['reason','Neden'],['strategy','Strateji']]);
  table(document.getElementById('events'), await api('/api/events'), [['t','Zaman'],['level','Seviye'],['code','Kod'],['message','Mesaj']]);
  document.getElementById('ops').textContent = JSON.stringify(await api('/api/ops'), null, 2);
  table(document.getElementById('changes'), await api('/api/config-changes'), [['timestamp','Zaman'],['path','Ayar'],['old','Önceki'],['new','Yeni'],['source','Kaynak'],['operator','Kim']]);
  const eq = await api('/api/equity');
  Plotly.react('equity', [{x: eq.map(e=>e.t), y: eq.map(e=>Number(e.equity)), type:'scatter', mode:'lines'}], {margin:{t:10,l:50,r:10,b:30}});
}
document.getElementById('kill').onclick = async () => { if(confirm('Kill switch tetiklensin mi? Yeni emirler durur.')){ const r = await api('/api/kill','POST'); alert(r.message); refresh(); } };
document.getElementById('resume').onclick = async () => { if(confirm('Bot devam etsin mi?')){ const r = await api('/api/resume','POST'); alert(r.message); refresh(); } };
refresh(); setInterval(refresh, 15000);
</script></body></html>"""


def _require(request: Request) -> str:
    panel_auth: PanelAuth = request.app.state.auth
    return panel_auth.authorize(request)


Auth = Annotated[str, Depends(_require)]


def create_app(control: BotControl, token: SecretStr) -> FastAPI:
    auth = PanelAuth(token)
    app = FastAPI(title="Kripto bot paneli", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        resp: Response = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.plot.ly; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
        )
        return resp

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
                LOGIN_HTML.replace("{error}", "<p style=color:#c0392b>Hatalı token</p>"),
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

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        try:
            auth.authorize(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(DASHBOARD_HTML)

    # ---------------------------------------------------------------- JSON API
    @app.get("/api/status")
    async def status(_: Auth) -> dict[str, Any]:
        return (await control.status()).to_dict()

    @app.get("/api/positions")
    async def positions(_: Auth) -> list[dict[str, Any]]:
        return await control.positions()

    @app.get("/api/trades")
    async def trades(_: Auth) -> list[dict[str, Any]]:
        return await control.recent_trades()

    @app.get("/api/equity")
    async def equity(_: Auth) -> list[dict[str, str]]:
        return await control.equity_curve()

    @app.get("/api/events")
    async def events(_: Auth) -> list[dict[str, Any]]:
        return await control.events()

    @app.get("/api/ops")
    async def ops(_: Auth) -> dict[str, Any]:
        return control.ops_status()

    @app.get("/api/config-changes")
    async def config_changes(_: Auth) -> list[dict[str, Any]]:
        return await control.config_changes()

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

    # ---------------------------------------------------------------- analysis reports
    @app.get("/reports", response_class=HTMLResponse)
    async def reports(request: Request) -> Response:
        try:
            auth.authorize(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        rows = []
        for md in control.reports()[:200]:
            chart = md.with_suffix(".html")
            link_chart = (
                f' · <a href="/reports/{html.escape(chart.name)}">grafik</a>'
                if chart.exists()
                else ""
            )
            rows.append(
                f'<li><a href="/reports/{html.escape(md.name)}">{html.escape(md.stem)}</a>'
                f"{link_chart}</li>"
            )
        body = "".join(rows) or "<li>Henüz rapor yok.</li>"
        page = f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Analiz raporları</title>
<style>body{{font-family:system-ui;max-width:900px;margin:auto;padding:16px}}</style></head><body>
<p><a href="/">← Panel</a></p><h1>Analiz raporları</h1>
<form id="f"><input name="symbol" placeholder="BTC/USDT" required> <button>Analiz et</button></form>
<pre id="out"></pre><ul>{body}</ul>
<script>
const csrf = document.cookie.split('; ').find(c=>c.startsWith('tb_csrf='))?.split('=')[1] || '';
document.getElementById('f').onsubmit = async e => {{ e.preventDefault();
  const s = new FormData(e.target).get('symbol');
  document.getElementById('out').textContent = 'Analiz ediliyor…';
  const r = await fetch('/api/analyze?symbol=' + encodeURIComponent(s), {{method:'POST', headers:{{'X-CSRF-Token': csrf}}}});
  const j = await r.json(); document.getElementById('out').textContent = j.summary || j.detail;
  if (r.ok) setTimeout(() => location.reload(), 800); }};
</script></body></html>"""
        return HTMLResponse(page)

    @app.get("/reports/{name}")
    async def report_file(name: str, _: Auth) -> Response:
        path: Path | None = control.report_file(name)
        if path is None:
            raise HTTPException(404, "Rapor bulunamadı")
        if path.suffix == ".html":
            return FileResponse(path, media_type="text/html")
        text = html.escape(path.read_text(encoding="utf-8"))
        return HTMLResponse(
            f'<!doctype html><meta charset="utf-8"><p><a href="/reports">← Raporlar</a>'
            f'</p><pre style="white-space:pre-wrap;font-family:system-ui">{text}</pre>'
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app
