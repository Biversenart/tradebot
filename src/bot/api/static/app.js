// Kripto bot paneli — çekirdek: API istemcisi, yönlendirme, canlı olaylar, ortak bileşenler.
// Satır içi script/olay işleyici yok (CSP: script-src 'self').
import { PAGES } from "./pages.js";

// ------------------------------------------------------------------ utils
export const $ = (sel, root = document) => root.querySelector(sel);
export const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const nf = (d) => new Intl.NumberFormat("tr-TR", { minimumFractionDigits: d, maximumFractionDigits: d });
export function num(v, digits = 2) {
  if (v === null || v === undefined || v === "") return "–";
  const n = Number(v);
  return Number.isFinite(n) ? nf(digits).format(n) : esc(v);
}
export function money(v, digits = 2) { return num(v, digits); }
export function signed(v, digits = 2, suffix = "") {
  if (v === null || v === undefined || v === "") return "–";
  const n = Number(v);
  const cls = n > 0 ? "pos" : n < 0 ? "neg" : "";
  return `<span class="${cls}">${n > 0 ? "+" : ""}${num(n, digits)}${suffix}</span>`;
}
export function qty(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return esc(v);
  return n === 0 ? "0" : Math.abs(n) >= 100 ? num(n, 2) : Math.abs(n) >= 1 ? num(n, 4) : num(n, 6);
}
export function when(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toLocaleString("tr-TR", { dateStyle: "short", timeStyle: "short" });
}
export function ago(iso) {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (!isFinite(s)) return "";
  if (s < 60) return "az önce";
  if (s < 3600) return `${Math.floor(s / 60)} dk önce`;
  if (s < 86400) return `${Math.floor(s / 3600)} sa önce`;
  return `${Math.floor(s / 86400)} gün önce`;
}
export const sideLabel = (s) =>
  s === "long" || s === "buy" ? '<span class="pos">LONG</span>' : '<span class="neg">SHORT</span>';

// ------------------------------------------------------------------ API
const csrf = () => document.cookie.split("; ").find((c) => c.startsWith("tb_csrf="))?.split("=")[1] || "";

export async function api(path, { method = "GET", body } = {}) {
  const opts = { method, credentials: "same-origin", headers: { "X-CSRF-Token": csrf() } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401) {
    location.href = "/login";
    throw new Error("Oturum sona erdi");
  }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const msg = typeof data === "object" ? (Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail) : data;
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return data;
}

// ------------------------------------------------------------------ UI helpers
export function toast(text, level = "info", ms = 6000) {
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms);
}

export function confirmBox(text, okLabel = "Onayla") {
  const dlg = $("#confirm-dialog");
  $("#confirm-text").textContent = text;
  $("#confirm-ok").textContent = okLabel;
  dlg.showModal();
  return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
}

/** rows: array of objects; cols: [{key,label,num?,fmt?(value,row)}] */
export function table(rows, cols, empty = "Kayıt yok.") {
  if (!rows || !rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const head = cols.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows
    .map(
      (r) =>
        "<tr>" +
        cols
          .map((c) => {
            const v = r[c.key];
            const html = c.fmt ? c.fmt(v, r) : esc(v ?? "–");
            return `<td class="${c.num ? "num" : ""} ${c.wrap ? "wrap" : ""}">${html}</td>`;
          })
          .join("") +
        "</tr>"
    )
    .join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

export function kpi(label, value, sub = "") {
  return `<div class="card kpi"><div class="label">${esc(label)}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
}

export function meter(name, value, limit, suffix = "%", digits = 2) {
  const v = Number(value) || 0;
  const l = Number(limit) || 0;
  const ratio = l > 0 ? Math.min(1, v / l) : 0;
  const cls = ratio >= 0.9 ? "bad" : ratio >= 0.6 ? "warn" : "";
  return `<div class="meter"><div class="head"><span>${esc(name)}</span><span class="mono">${num(v, digits)}${suffix} / ${num(l, digits)}${suffix}</span></div>
    <div class="bar"><div class="fill ${cls}" style="width:${(ratio * 100).toFixed(1)}%"></div></div></div>`;
}

export function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function plotLayout(extra = {}) {
  return {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: css("--text"), size: 12 },
    margin: { t: 10, r: 10, b: 30, l: 55 },
    xaxis: { gridcolor: css("--border"), zeroline: false },
    yaxis: { gridcolor: css("--border"), zeroline: false },
    legend: { orientation: "h", y: 1.08 },
    hovermode: "x unified",
    ...extra,
  };
}

export function plot(el, traces, layout = {}) {
  if (!window.Plotly) {
    el.innerHTML = '<div class="empty">Grafik kütüphanesi yüklenemedi.</div>';
    return;
  }
  window.Plotly.react(el, traces, plotLayout(layout), { displaylogo: false, responsive: true });
}

// ------------------------------------------------------------------ app state
export const state = { status: null, timers: [], cleanup: [], gen: 0 };

/** Run a page refresh; errors from a page the user already left are ignored. */
function guarded(fn) {
  const gen = state.gen;
  Promise.resolve()
    .then(fn)
    .catch((e) => { if (gen === state.gen) toast(e.message, "error"); });
}

export function every(ms, fn) {
  state.timers.push(setInterval(() => guarded(fn), ms));
}
export function onLeave(fn) {
  state.cleanup.push(fn);
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    state.status = s;
    const mode = String(s.mode).toUpperCase();
    const modeCls = mode === "LIVE" ? "bad" : mode === "TESTNET" ? "warn" : "info";
    const ks = s.kill_switch_active
      ? `<span class="pill bad">⛔ Kill switch AKTİF</span>`
      : `<span class="pill good">● İşlem açık</span>`;
    const ip = s.egress_ok ? `<span class="pill good">IP doğrulandı</span>` : `<span class="pill warn">IP DOĞRULANMADI</span>`;
    $("#status-pills").innerHTML = `<span class="pill ${modeCls}">${esc(mode)}</span>${ks}${ip}<span class="pill">Pozisyon: ${s.open_positions}</span>`;
    const banner = $("#banner");
    if (s.kill_switch_active) {
      banner.hidden = false;
      banner.className = "banner";
      banner.textContent = "Kill switch aktif — yeni emir gönderilmiyor. Nedenler: " + Object.values(s.kill_reasons).join(" · ");
    } else if (!s.egress_ok && mode !== "PAPER") {
      banner.hidden = false;
      banner.className = "banner warn";
      banner.textContent = "Sabit çıkış IP'si doğrulanmadı — emirler kapalı (fail-closed).";
    } else {
      banner.hidden = true;
    }
    $("#resume-btn").hidden = !s.kill_switch_active;
  } catch (e) {
    $("#status-pills").innerHTML = `<span class="pill warn">Bağlantı yok: ${esc(e.message)}</span>`;
  }
}

// ------------------------------------------------------------------ router
let current = null;

async function route() {
  state.timers.forEach(clearInterval);
  state.timers = [];
  state.cleanup.forEach((f) => { try { f(); } catch { /* ignore */ } });
  state.cleanup = [];
  state.gen += 1;
  const gen = state.gen;
  const hash = location.hash.replace(/^#\/?/, "");
  const [slug, query] = hash.split("?");
  const page = PAGES.find((p) => p.slug === (slug || "")) || PAGES[0];
  current = page;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page.id));
  $("#page-title").textContent = page.title;
  document.title = `${page.title} · Kripto bot`;
  $("#sidebar").classList.remove("open");
  const view = $("#view");
  view.innerHTML = '<div class="empty"><span class="spinner"></span> Yükleniyor…</div>';
  try {
    await page.render(view, new URLSearchParams(query || ""));
  } catch (e) {
    if (gen === state.gen) view.innerHTML = `<div class="card"><h2>Hata</h2><p class="neg">${esc(e.message)}</p></div>`;
  }
}

export function refreshPage() {
  const page = current;
  if (page?.refresh) guarded(() => page.refresh());
}

// ------------------------------------------------------------------ live events (SSE)
function connectStream() {
  if (!window.EventSource) return;
  const es = new EventSource("/api/stream");
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.type === "alert") {
      toast(ev.message, ev.level === "critical" ? "critical" : ev.level);
    } else if (ev.type === "position") {
      const kinds = { opened: "açıldı", reduced: "azaltıldı", closed: "kapandı" };
      const pnl = ev.kind === "closed" ? ` · PnL ${num(ev.pnl)}` : "";
      toast(`${ev.symbol} ${String(ev.side).toUpperCase()} ${kinds[ev.kind] || ev.kind}${pnl}`, ev.kind === "closed" && Number(ev.pnl) < 0 ? "warning" : "success");
    }
    refreshStatus();
    refreshPage();
  };
}

// ------------------------------------------------------------------ boot
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("tb-theme", t); } catch { /* private mode */ }
}

function boot() {
  let theme = "dark";
  try { theme = localStorage.getItem("tb-theme") || "dark"; } catch { /* ignore */ }
  applyTheme(theme);
  $("#theme-toggle").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
    route();
  });
  $("#menu-toggle").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#kill-btn").addEventListener("click", async () => {
    if (!(await confirmBox("Kill switch tetiklensin mi? Yeni emirler durur; açık pozisyonlar config'e göre kapatılır.", "Durdur"))) return;
    try {
      const r = await api("/api/kill", { method: "POST" });
      toast(r.message, "critical");
    } catch (e) { toast(e.message, "error"); }
    refreshStatus();
    refreshPage();
  });
  $("#resume-btn").addEventListener("click", async () => {
    if (!(await confirmBox("Bot devam etsin mi? (Otomatik nedenler — günlük zarar, IP — kendiliğinden kalkar.)", "Devam"))) return;
    try {
      const r = await api("/api/resume", { method: "POST" });
      toast(r.message, "success");
    } catch (e) { toast(e.message, "error"); }
    refreshStatus();
    refreshPage();
  });
  window.addEventListener("hashchange", route);
  refreshStatus();
  setInterval(refreshStatus, 10000);
  connectStream();
  route();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
