// Panel sayfaları. Her sayfa: { id, slug, title, render(view, params), refresh? }
import {
  $, ago, api, confirmBox, css, esc, every, kpi, meter, money, num, plot, qty,
  sideLabel, signed, state, table, toast, when,
} from "./app.js";

const ccy = () => esc(state.status?.base_currency || "USDT");

// ================================================================== Genel bakış
const overview = {
  id: "overview",
  slug: "",
  title: "Genel bakış",
  async render(view) {
    view.innerHTML = `
      <div class="grid kpis" id="kpis"></div>
      <div class="grid two">
        <div class="card"><h2>Bakiye ve drawdown</h2><div id="eq" class="chart"></div></div>
        <div class="card"><h2>Günlük gerçekleşen PnL</h2><div id="daily" class="chart"></div></div>
      </div>
      <div class="grid two">
        <div class="card flush"><h2>Açık pozisyonlar</h2><div id="pos"></div></div>
        <div class="card flush"><h2>Son olaylar</h2><div id="ev"></div></div>
      </div>`;
    await this.refresh();
    every(15000, () => this.refresh());
  },
  async refresh() {
    const [o, perf, pos, ev] = await Promise.all([
      api("/api/overview"), api("/api/performance"), api("/api/positions"), api("/api/events?limit=10"),
    ]);
    const t = o.totals || {};
    $("#kpis").innerHTML = [
      kpi("Bakiye", money(o.equity), `${esc(o.base_currency)} · rezerv ${money(o.reserve)}`),
      kpi("Bugün (gerçekleşen)", signed(o.today_pnl), "UTC gün"),
      kpi("Açık PnL", signed(o.unrealized_pnl), `${pos.length} pozisyon`),
      kpi("Toplam gerçekleşen", signed(t.pnl), `${t.trades || 0} işlem`),
      kpi("İsabet oranı", t.win_rate == null ? "–" : `%${num(t.win_rate, 1)}`, `PF ${t.profit_factor ?? "–"} · E ${t.expectancy_r ?? "–"}R`),
      kpi("Maruziyet", o.exposure_pct == null ? "–" : `%${num(o.exposure_pct)}`, money(o.exposure)),
      kpi("Drawdown", `%${num(o.current_drawdown_pct)}`, `Maks. %${num(o.max_drawdown_pct)}`),
      kpi("Mod", esc(String(o.mode).toUpperCase()), o.live_confirmed ? '<span class="neg">GERÇEK EMİR</span>' : "gerçek emir yok"),
    ].join("");
    const eq = perf.equity || [];
    if (eq.length) {
      plot($("#eq"), [
        { x: eq.map((e) => e.t), y: eq.map((e) => +e.equity), type: "scatter", mode: "lines", name: "Bakiye", line: { color: css("--accent"), width: 2 } },
        { x: eq.map((e) => e.t), y: eq.map((e) => -e.dd), type: "scatter", mode: "lines", name: "Drawdown %", yaxis: "y2", fill: "tozeroy", line: { color: css("--bad"), width: 1 } },
      ], {
        yaxis: { domain: [0.32, 1], gridcolor: css("--border") },
        yaxis2: { domain: [0, 0.24], gridcolor: css("--border"), ticksuffix: "%" },
        showlegend: false,
      });
    } else $("#eq").innerHTML = '<div class="empty">Henüz bakiye kaydı yok.</div>';
    const d = perf.daily_pnl || [];
    if (d.length) {
      plot($("#daily"), [{ x: d.map((r) => r.day), y: d.map((r) => +r.pnl), type: "bar", name: "PnL", marker: { color: d.map((r) => (+r.pnl >= 0 ? css("--good") : css("--bad"))) } }], { hovermode: "closest" });
    } else $("#daily").innerHTML = '<div class="empty">Kapanmış işlem yok.</div>';
    $("#pos").innerHTML = table(pos, [
      { key: "symbol", label: "Sembol" },
      { key: "side", label: "Yön", fmt: sideLabel },
      { key: "amount", label: "Miktar", num: true, fmt: qty },
      { key: "entry", label: "Giriş", num: true, fmt: (v) => num(v, 4) },
      { key: "mark", label: "Fiyat", num: true, fmt: (v) => num(v, 4) },
      { key: "unrealized_pnl", label: "Açık PnL", num: true, fmt: (v) => signed(v) },
      { key: "r", label: "R", num: true, fmt: (v) => signed(v, 2, "R") },
    ], "Açık pozisyon yok.");
    $("#ev").innerHTML = eventsTable(ev);
  },
};

function levelPill(l) {
  const cls = l === "critical" ? "bad" : l === "warning" ? "warn" : "info";
  return `<span class="pill ${cls}">${esc(l)}</span>`;
}
function eventsTable(rows) {
  return table(rows, [
    { key: "t", label: "Zaman", fmt: (v) => `<span title="${esc(when(v))}">${esc(ago(v))}</span>` },
    { key: "level", label: "Seviye", fmt: levelPill },
    { key: "code", label: "Kod", fmt: (v) => `<span class="mono small">${esc(v)}</span>` },
    { key: "message", label: "Mesaj", wrap: true },
  ], "Olay yok.");
}

// ================================================================== Pozisyonlar
const positions = {
  id: "positions",
  slug: "pozisyonlar",
  title: "Pozisyonlar ve işlemler",
  filters: { symbol: "", strategy: "" },
  async render(view) {
    view.innerHTML = `
      <div class="card flush"><h2>Açık pozisyonlar</h2><div id="open"></div></div>
      <div class="card flush">
        <div class="row" style="padding:16px 16px 0"><h2 style="margin:0">Kapanmış işlemler</h2><span class="spacer"></span>
          <form class="inline" id="flt">
            <input name="symbol" placeholder="Sembol (BTC/USDT)" size="12">
            <input name="strategy" placeholder="Strateji" size="12">
            <button class="btn small">Filtrele</button>
            <a class="btn small ghost" id="csv" href="/api/trades.csv">⬇ CSV</a>
          </form></div>
        <div id="stats" class="row" style="padding:10px 16px"></div>
        <div id="closed"></div>
      </div>`;
    $("#flt").addEventListener("submit", (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      this.filters = { symbol: String(f.get("symbol")).trim().toUpperCase(), strategy: String(f.get("strategy")).trim() };
      this.refresh();
    });
    await this.refresh();
    every(15000, () => this.refresh());
  },
  async refresh() {
    const q = new URLSearchParams({ limit: "500" });
    if (this.filters.symbol) q.set("symbol", this.filters.symbol);
    if (this.filters.strategy) q.set("strategy", this.filters.strategy);
    const csvQ = new URLSearchParams(q); csvQ.delete("limit");
    $("#csv").href = `/api/trades.csv?${csvQ}`;
    const [open, closed] = await Promise.all([api("/api/positions"), api(`/api/trades?${q}`)]);
    $("#open").innerHTML = table(open, [
      { key: "symbol", label: "Sembol", fmt: (v) => `<a href="#/grafik?symbol=${encodeURIComponent(v)}">${esc(v)}</a>` },
      { key: "side", label: "Yön", fmt: sideLabel },
      { key: "strategy", label: "Strateji" },
      { key: "amount", label: "Miktar", num: true, fmt: qty },
      { key: "entry", label: "Giriş", num: true, fmt: (v) => num(v, 4) },
      { key: "mark", label: "Fiyat", num: true, fmt: (v, r) => num(v, 4) + (r.mark_live ? "" : ' <span class="muted small" title="canlı fiyat yok">*</span>') },
      { key: "stop", label: "Stop", num: true, fmt: (v, r) => num(v, 4) + (r.stop_on_exchange ? ' <span title="Stop borsada">🛡️</span>' : ' <span class="neg" title="Borsada stop yok!">⚠</span>') },
      { key: "targets", label: "Hedefler", fmt: (v, r) => `${r.targets_hit}/${(v || []).length} · ${(v || []).map((x) => num(x, 2)).join(" / ")}` },
      { key: "notional", label: "Tutar", num: true, fmt: (v) => money(v) },
      { key: "unrealized_pnl", label: "Açık PnL", num: true, fmt: (v) => signed(v) },
      { key: "realized_pnl", label: "Gerçekleşen", num: true, fmt: (v) => signed(v) },
      { key: "r", label: "R", num: true, fmt: (v) => signed(v, 2, "R") },
      { key: "opened_at", label: "Açılış", fmt: when },
    ], "Açık pozisyon yok.");
    const wins = closed.filter((t) => +t.pnl > 0).length;
    const pnl = closed.reduce((a, t) => a + +t.pnl, 0);
    $("#stats").innerHTML = closed.length
      ? `<span class="pill">${closed.length} işlem</span><span class="pill">İsabet %${num((wins / closed.length) * 100, 1)}</span><span class="pill">${signed(pnl)} ${ccy()}</span>`
      : "";
    $("#closed").innerHTML = table(closed, [
      { key: "closed_at", label: "Kapanış", fmt: when },
      { key: "symbol", label: "Sembol" },
      { key: "side", label: "Yön", fmt: sideLabel },
      { key: "strategy", label: "Strateji" },
      { key: "entry", label: "Giriş", num: true, fmt: (v) => num(v, 4) },
      { key: "amount", label: "Miktar", num: true, fmt: qty },
      { key: "pnl", label: "PnL", num: true, fmt: (v) => signed(v) },
      { key: "fees", label: "Komisyon", num: true, fmt: (v) => num(v, 4) },
      { key: "r", label: "R", num: true, fmt: (v) => signed(v, 2, "R") },
      { key: "reason", label: "Neden" },
    ], "Kapanmış işlem yok.");
  },
};

// ================================================================== Grafik
const chart = {
  id: "chart",
  slug: "grafik",
  title: "Fiyat grafiği",
  sel: { symbol: "", tf: "1h", limit: "300" },
  async render(view, params) {
    const meta = await api("/api/symbols");
    this.sel.symbol = params.get("symbol") || this.sel.symbol || meta.symbols[0] || "BTC/USDT";
    const tfs = meta.timeframes.length ? meta.timeframes : ["1h"];
    if (!tfs.includes(this.sel.tf)) this.sel.tf = tfs.includes("1h") ? "1h" : tfs[0];
    const opt = (list, cur) => list.map((x) => `<option ${x === cur ? "selected" : ""}>${esc(x)}</option>`).join("");
    view.innerHTML = `
      <div class="card">
        <form class="inline" id="sel">
          <label class="field">Sembol<input name="symbol" list="syms" value="${esc(this.sel.symbol)}" size="12"></label>
          <datalist id="syms">${meta.symbols.map((s) => `<option value="${esc(s)}">`).join("")}</datalist>
          <label class="field">Zaman dilimi<select name="tf">${opt(tfs, this.sel.tf)}</select></label>
          <label class="field">Mum sayısı<select name="limit">${opt(["100", "300", "500", "1000", "2000"], this.sel.limit)}</select></label>
          <button class="btn primary">Göster</button>
          <span class="spacer"></span><span class="muted small" id="src"></span>
        </form>
      </div>
      <div class="card"><div id="candles" class="chart tall"></div>
        <p class="muted small">▲ giriş (long) · ▼ giriş (short) · ✕ çıkış · kesikli çizgiler: açık pozisyonun giriş / stop / hedefleri. Kaynak: canlı mum tamponu, yoksa indirilmiş Parquet verisi.</p></div>`;
    $("#sel").addEventListener("submit", (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      this.sel = { symbol: String(f.get("symbol")).trim().toUpperCase(), tf: String(f.get("tf")), limit: String(f.get("limit")) };
      this.refresh();
    });
    await this.refresh();
    every(30000, () => this.refresh());
  },
  async refresh() {
    const q = new URLSearchParams({ symbol: this.sel.symbol, tf: this.sel.tf, limit: this.sel.limit });
    const d = await api(`/api/candles?${q}`);
    $("#src").textContent = `${d.symbol} · ${d.timeframe} · ${d.source} · ${d.bars.length} mum`;
    const el = $("#candles");
    if (!d.bars.length) {
      el.innerHTML = `<div class="empty">Bu sembol/zaman dilimi için veri yok. Backtest &amp; veri sayfasından indirebilirsiniz.</div>`;
      return;
    }
    const x = d.bars.map((b) => b.t);
    const traces = [
      { x, open: d.bars.map((b) => b.o), high: d.bars.map((b) => b.h), low: d.bars.map((b) => b.l), close: d.bars.map((b) => b.c), type: "candlestick", name: d.symbol, increasing: { line: { color: css("--good") } }, decreasing: { line: { color: css("--bad") } } },
      { x, y: d.bars.map((b) => b.v), type: "bar", name: "Hacim", yaxis: "y2", marker: { color: css("--border") }, opacity: 0.6 },
    ];
    const first = new Date(x[0]);
    const mk = d.markers.filter((m) => new Date(m.t) >= first);
    const group = (pred, name, symbol, color) => {
      const pts = mk.filter(pred);
      if (pts.length) traces.push({ x: pts.map((m) => m.t), y: pts.map((m) => +m.price), mode: "markers", type: "scatter", name, text: pts.map((m) => m.label), marker: { symbol, size: 12, color, line: { width: 1, color: "#000" } } });
    };
    group((m) => m.kind === "entry" && m.side === "long", "Giriş (long)", "triangle-up", css("--good"));
    group((m) => m.kind === "entry" && m.side !== "long", "Giriş (short)", "triangle-down", css("--bad"));
    group((m) => m.kind === "exit", "Çıkış", "x", css("--warn"));
    const shapes = [];
    const hline = (y, color, dash = "dash") => shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, y0: +y, y1: +y, line: { color, width: 1, dash } });
    for (const p of d.open) {
      hline(p.entry, css("--accent"));
      hline(p.stop, css("--bad"));
      (p.targets || []).forEach((t) => hline(t, css("--good"), "dot"));
    }
    plot(el, traces, {
      xaxis: { rangeslider: { visible: false }, gridcolor: css("--border") },
      yaxis: { domain: [0.22, 1], gridcolor: css("--border") },
      yaxis2: { domain: [0, 0.18], showgrid: false },
      shapes,
      hovermode: "x",
    });
  },
};

// ================================================================== Stratejiler
const strategies = {
  id: "strategies",
  slug: "stratejiler",
  title: "Stratejiler",
  async render(view) {
    view.innerHTML = `
      <div class="card"><h2>Strateji başına gerçekleşen PnL</h2><div id="perf" class="chart"></div></div>
      <div class="grid three" id="cards"></div>
      <div class="card"><h2>Gölge mod karşılaştırması</h2><div id="shadow"></div>
        <p class="muted small">Değişen parametreler önce gölgede (sanal) çalışır. Onaylanan sürüm bir sonraki yeniden başlatmada canlıya alınır.</p></div>`;
    await this.refresh();
  },
  async refresh() {
    const [list, perf, ops] = await Promise.all([api("/api/strategies"), api("/api/performance"), api("/api/ops")]);
    const by = perf.by_strategy || {};
    const names = Object.keys(by);
    if (names.length) {
      plot($("#perf"), [{ x: names, y: names.map((n) => +by[n].pnl), type: "bar", marker: { color: names.map((n) => (+by[n].pnl >= 0 ? css("--good") : css("--bad"))) } }], { hovermode: "closest" });
    } else $("#perf").innerHTML = '<div class="empty">Henüz kapanmış işlem yok.</div>';
    $("#cards").innerHTML = list.length
      ? list.map((s) => {
          const st = s.stats || {};
          const params = Object.entries(s.params).filter(([k]) => k !== "timeframe").map(([k, v]) => `${esc(k)}=${esc(v)}`).join(", ");
          return `<div class="card">
            <div class="row"><h2 style="margin:0">${esc(s.name)}</h2><span class="badge">${esc(s.symbol)}</span><span class="badge">${esc(s.timeframe || "–")}</span><span class="spacer"></span>
              <label class="switch" title="Aç/kapat"><input type="checkbox" data-key="${esc(s.key)}" ${s.enabled ? "checked" : ""}><span></span></label></div>
            <p class="muted small mono">${params || "varsayılan parametreler"}</p>
            <div class="row small"><span class="pill">${st.trades ?? 0} işlem</span><span class="pill">İsabet ${st.win_rate == null ? "–" : "%" + num(st.win_rate, 1)}</span>
              <span class="pill">PF ${esc(st.profit_factor ?? "–")}</span><span class="pill">E ${esc(st.expectancy_r ?? "–")}R</span><span class="pill">${signed(st.pnl ?? 0)}</span></div>
            ${s.shadow ? `<p class="small">🌗 ${esc(s.shadow)}</p>` : ""}
          </div>`;
        }).join("")
      : '<div class="card empty">Etkin strateji yok (config.yaml → strategies).</div>';
    document.querySelectorAll("#cards input[data-key]").forEach((cb) =>
      cb.addEventListener("change", async () => {
        try {
          await api(`/api/strategies/${encodeURIComponent(cb.dataset.key)}?enabled=${cb.checked}`, { method: "POST" });
          toast(`${cb.dataset.key} ${cb.checked ? "açıldı" : "kapatıldı"}`, "success");
        } catch (e) { toast(e.message, "error"); cb.checked = !cb.checked; }
      })
    );
    const shadow = ops.shadow || [];
    $("#shadow").innerHTML = shadow.length
      ? `<ul>${shadow.map((line) => {
          const name = line.split(":")[0];
          return `<li class="row"><span class="small">${esc(line)}</span><button class="btn small" data-approve="${esc(name)}">Onayla</button></li>`;
        }).join("")}</ul>`
      : '<div class="empty">Gölgede çalışan strateji yok.</div>';
    document.querySelectorAll("[data-approve]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!(await confirmBox(`${b.dataset.approve} için gölgedeki parametreler onaylansın mı? Yeniden başlatınca canlıda çalışır.`))) return;
        try { toast((await api(`/api/shadow/${encodeURIComponent(b.dataset.approve)}/approve`, { method: "POST" })).message, "success"); }
        catch (e) { toast(e.message, "error"); }
      })
    );
  },
};

// ================================================================== Risk
const risk = {
  id: "risk",
  slug: "risk",
  title: "Risk ve güvenlik",
  async render(view) {
    view.innerHTML = `<div class="grid two">
        <div class="card"><h2>Kill switch</h2><div id="ks"></div></div>
        <div class="card"><h2>Limitler</h2><div id="limits"></div></div>
      </div>
      <div class="grid two">
        <div class="card flush"><h2>Parite maruziyeti</h2><div id="expo"></div></div>
        <div class="card"><h2>Boyutlama (son sinyal)</h2><div id="sizing"></div></div>
      </div>
      <div class="grid two">
        <div class="card"><h2>Operasyonel güvenlik</h2><div id="ops"></div></div>
        <div class="card"><h2>Kanarya sermaye tavanı</h2><div id="cap"></div>
          <form class="inline" id="capf"><label class="field">Yeni tavan (%)<input name="pct" type="number" min="1" max="100" step="1" required></label><button class="btn">Uygula</button></form>
          <p class="muted small">Yalnızca live modda uygulanır. Artış: son değişiklikten en az kanarya süresi sonra ve tek seferde en fazla x2. Azaltma her zaman serbest.</p></div>
      </div>`;
    $("#capf").addEventListener("submit", async (e) => {
      e.preventDefault();
      const pct = new FormData(e.target).get("pct");
      if (!(await confirmBox(`Sermaye tavanı %${pct} yapılsın mı?`))) return;
      try { const r = await api(`/api/capital-cap?pct=${encodeURIComponent(pct)}`, { method: "POST" }); toast(r.message, r.message.startsWith("Reddedildi") ? "warning" : "success"); }
      catch (err) { toast(err.message, "error"); }
      this.refresh();
    });
    await this.refresh();
    every(15000, () => this.refresh());
  },
  async refresh() {
    const r = await api("/api/risk");
    const k = r.kill_switch;
    const reasons = Object.entries(k.reasons);
    $("#ks").innerHTML = `
      <p>${k.active ? '<span class="pill bad">AKTİF</span>' : '<span class="pill good">Kapalı — işlem açık</span>'}
        ${k.close_requested ? '<span class="pill warn">pozisyon kapatma istendi</span>' : ""}</p>
      ${reasons.length ? `<ul>${reasons.map(([c, m]) => `<li><b>${esc(c)}</b>: ${esc(m)}</li>`).join("")}</ul>` : ""}
      <p class="muted small">Tetiklenince: <b>${esc(k.on_kill_switch)}</b>${k.triggered_at ? ` · ${esc(when(k.triggered_at))}` : ""}</p>
      ${meter("Art arda API hatası", r.errors.consecutive, r.errors.max, "", 0)}
      ${meter("Açık pozisyon", r.open_positions.value, r.open_positions.max, "", 0)}`;
    $("#limits").innerHTML = r.limits.map((l) => meter(l.name, l.value_pct, l.limit_pct)).join("") +
      `<p class="muted small">İşlem başı risk %${esc(r.settings.risk_per_trade_pct)} · kaldıraç x${esc(r.settings.max_leverage)} · maks. spread %${esc(r.settings.max_spread_pct)} · maks. korelasyon ${esc(r.settings.max_correlation)}</p>`;
    $("#expo").innerHTML = table(r.exposure, [
      { key: "symbol", label: "Sembol" },
      { key: "notional", label: "Tutar", num: true, fmt: (v) => money(v) },
      { key: "pct", label: "Bakiye %", num: true, fmt: (v, row) => `${num(v)} / ${num(row.limit_pct)}` },
    ], "Açık maruziyet yok.");
    const s = r.sizing;
    if (!s) $("#sizing").innerHTML = '<div class="empty">Boyutlayıcı bilgisi yok.</div>';
    else {
      const b = s.last;
      $("#sizing").innerHTML = `<p class="small">Başlangıç ${money(s.initial_equity)} · tepe ${money(s.peak)} · rezerv ${money(s.reserve)} · kayıp serisi ${s.loss_streak}</p>` +
        (b ? table([b], [
          { key: "base", label: "Taban %", num: true },
          { key: "quality", label: "Kalite", num: true },
          { key: "drawdown", label: "DD", num: true },
          { key: "loss_streak", label: "Seri", num: true },
          { key: "event", label: "Olay", num: true },
          { key: "allocation", label: "Tahsis", num: true },
          { key: "final_risk_pct", label: "Sonuç %", num: true },
        ]) + (b.paused_reason ? `<p class="neg small">Duraklatıldı: ${esc(b.paused_reason)}</p>` : "") + (b.notes?.length ? `<p class="muted small">${b.notes.map(esc).join(" · ")}</p>` : "")
          : '<div class="empty">Henüz sinyal boyutlanmadı.</div>');
    }
    const o = r.ops || {};
    const rows = [
      ["Stablecoin", o.depeg === "normal" ? '<span class="pos">normal</span>' : `<span class="neg">${esc(o.depeg)}</span>`],
      ["Saat farkı (ms)", Object.entries(o.clock_offsets_ms || {}).map(([k, v]) => `${esc(k)}: ${num(v, 0)}`).join(", ") || "–"],
      ["Saat engeli", (o.clock_blocked || []).map(esc).join(", ") || '<span class="pos">yok</span>'],
      ["Delisting", Object.entries(o.delisted || {}).map(([k, v]) => `${esc(k)}: ${v.length ? v.map(esc).join(", ") : "yok"}`).join(" · ") || "–"],
    ];
    $("#ops").innerHTML = `<div class="table-wrap"><table><tbody>${rows.map(([a, b]) => `<tr><th>${esc(a)}</th><td class="wrap">${b}</td></tr>`).join("")}</tbody></table></div>`;
    $("#cap").innerHTML = `<p>${esc(o.capital_cap || "–")}</p>`;
  },
};

// ================================================================== Arbitraj
const arbitrage = {
  id: "arbitrage",
  slug: "arbitraj",
  title: "Arbitraj fırsatları",
  async render(view) {
    view.innerHTML = `<div class="grid kpis" id="akpi"></div>
      <div class="card"><h2>Net spread dağılımı</h2><div id="ahist" class="chart"></div></div>
      <div class="card flush"><h2>Son fırsatlar</h2><div id="atab"></div></div>
      <p class="muted small">v1: fırsatlar her modda loglanır; yürütme yalnızca paper modda yapılır.</p>`;
    await this.refresh();
    every(15000, () => this.refresh());
  },
  async refresh() {
    const rows = await api("/api/arbitrage?limit=500");
    const ex = rows.filter((r) => r.executed).length;
    const best = rows.reduce((a, r) => Math.max(a, +r.net_pct), -Infinity);
    $("#akpi").innerHTML = [
      kpi("Fırsat", num(rows.length, 0), "son 500"),
      kpi("Yürütülen (paper)", num(ex, 0)),
      kpi("En iyi net", rows.length ? `%${num(best, 3)}` : "–"),
      kpi("Çapraz / üçgen", `${rows.filter((r) => r.kind === "cross").length} / ${rows.filter((r) => r.kind !== "cross").length}`),
    ].join("");
    if (rows.length) plot($("#ahist"), [{ x: rows.map((r) => +r.net_pct), type: "histogram", marker: { color: css("--accent") } }], { hovermode: "closest", xaxis: { ticksuffix: "%", gridcolor: css("--border") } });
    else $("#ahist").innerHTML = '<div class="empty">Henüz fırsat yok (arbitrage.*.enabled?).</div>';
    $("#atab").innerHTML = table(rows.slice(0, 200), [
      { key: "t", label: "Zaman", fmt: when },
      { key: "kind", label: "Tür" },
      { key: "route", label: "Rota", wrap: true },
      { key: "net_pct", label: "Net %", num: true, fmt: (v) => signed(v, 3, "%") },
      { key: "size_quote", label: "Boyut", num: true, fmt: (v) => money(v) },
      { key: "executed", label: "Yürütüldü", fmt: (v) => (v ? "✔" : "–") },
    ], "Fırsat kaydı yok.");
  },
};

// ================================================================== Backtest & veri
const backtest = {
  id: "backtest",
  slug: "backtest",
  title: "Backtest ve veri",
  async render(view) {
    const meta = await api("/api/symbols");
    const strat = meta.strategies.map((s) => `<label class="row small"><input type="checkbox" name="strategies" value="${esc(s)}"> ${esc(s)}</label>`).join("");
    view.innerHTML = `<div class="grid two">
      <div class="card"><h2>Backtest çalıştır (walk-forward)</h2>
        <form id="btf" class="grid">
          <label class="field">Semboller (virgülle)<input name="symbols" value="${esc(meta.symbols.join(", "))}" required></label>
          <div class="row"><label class="field">Zaman dilimi<input name="timeframe" value="1h" size="5"></label>
            <label class="field">Başlangıç<input name="since" type="date" value="2022-01-01"></label>
            <label class="field">Bitiş<input name="until" type="date"></label></div>
          <div><div class="muted small">Stratejiler (boş: config'te etkin olanlar)</div><div class="row">${strat}</div></div>
          <div><button class="btn primary">▶ Başlat</button></div>
        </form>
        <p class="muted small">İndirilmiş Parquet verisini kullanır; sonuçlar spec §9 eşikleriyle karşılaştırılır ve önyargı kontrolleri raporlanır.</p></div>
      <div class="card"><h2>Geçmiş veri indir</h2>
        <form id="dlf" class="grid">
          <label class="field">Semboller (virgülle)<input name="symbols" value="${esc(meta.symbols.join(", "))}" required></label>
          <div class="row"><label class="field">Zaman dilimleri<input name="timeframes" value="1h" size="10"></label>
            <label class="field">Başlangıç<input name="since" type="date" value="2022-01-01" required></label>
            <label class="field">Bitiş<input name="until" type="date"></label></div>
          <div><button class="btn primary">⬇ İndir</button></div>
        </form>
        <p class="muted small">Borsanın herkese açık API'sinden, egress (sabit IP) üzerinden indirilir; kaldığı yerden devam eder.</p></div>
      </div>
      <div class="card flush"><h2>İşler</h2><div id="jobs"></div></div>
      <div class="grid two">
        <div class="card flush"><h2>Raporlar</h2><div id="runs"></div></div>
        <div class="card flush"><h2>İndirilmiş veri</h2><div id="data"></div></div>
      </div>
      <div class="card" id="viewer" hidden><div class="row"><h2 style="margin:0" id="vtitle"></h2><span class="spacer"></span><a class="btn small" id="vopen" target="_blank" rel="noopener">Yeni sekmede aç</a></div>
        <iframe class="report" id="vframe" title="Rapor"></iframe></div>`;
    const list = (v) => String(v || "").split(",").map((s) => s.trim()).filter(Boolean);
    $("#btf").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      try {
        await api("/api/jobs/backtest", { method: "POST", body: { symbols: list(f.get("symbols")), strategies: f.getAll("strategies"), timeframe: f.get("timeframe"), since: f.get("since") || null, until: f.get("until") || null } });
        toast("Backtest başladı.", "success");
      } catch (err) { toast(err.message, "error"); }
      this.refresh();
    });
    $("#dlf").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      try {
        await api("/api/jobs/download", { method: "POST", body: { symbols: list(f.get("symbols")), timeframes: list(f.get("timeframes")), since: f.get("since"), until: f.get("until") || null } });
        toast("İndirme başladı.", "success");
      } catch (err) { toast(err.message, "error"); }
      this.refresh();
    });
    await this.refresh();
    every(4000, () => this.refreshJobs());
  },
  show(url, title) {
    $("#viewer").hidden = false;
    $("#vtitle").textContent = title;
    $("#vframe").src = url;
    $("#vopen").href = url;
    $("#viewer").scrollIntoView({ behavior: "smooth" });
  },
  async refreshJobs() {
    let jobs;
    try { jobs = await api("/api/jobs"); } catch (e) { $("#jobs").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    const was = this.running;
    this.running = jobs.some((j) => j.status === "running" || j.status === "queued");
    $("#jobs").innerHTML = table(jobs, [
      { key: "created_at", label: "Başlangıç", fmt: when },
      { key: "kind", label: "Tür", fmt: (v) => (v === "backtest" ? "Backtest" : "Veri indirme") },
      { key: "params", label: "Parametreler", wrap: true, fmt: (p) => `<span class="small mono">${esc((p.symbols || []).join(", "))} · ${esc(p.timeframe || (p.timeframes || []).join(","))} · ${esc(p.since || "")}${p.until ? "→" + esc(p.until) : ""}</span>` },
      { key: "status", label: "Durum", fmt: (v) => ({ queued: '<span class="pill">sırada</span>', running: '<span class="pill info"><span class="spinner"></span> çalışıyor</span>', done: '<span class="pill good">tamam</span>', failed: '<span class="pill bad">hata</span>' })[v] || esc(v) },
      { key: "message", label: "Sonuç", wrap: true, fmt: (v, j) => esc(v) + (j.result?.run ? ` · <a href="#" data-run="${esc(j.result.run)}">raporu aç</a>` : "") + (j.result?.lines ? `<div class="small muted">${j.result.lines.map(esc).join("<br>")}</div>` : "") },
    ], "Henüz iş yok.");
    $("#jobs").querySelectorAll("[data-run]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); this.show(`/backtests/${encodeURIComponent(a.dataset.run)}/OZET.md`, `Rapor ${a.dataset.run}`); }));
    if (was && !this.running) this.refresh();
  },
  async refresh() {
    await this.refreshJobs();
    const [runs, data] = await Promise.all([api("/api/backtests").catch(() => []), api("/api/datasets").catch(() => [])]);
    $("#runs").innerHTML = table(runs, [
      { key: "run", label: "Çalıştırma" },
      { key: "files", label: "Dosyalar", wrap: true, fmt: (files, r) => files.filter((f) => /\.(md|html)$/.test(f)).map((f) => `<a href="#" data-file="${esc(r.run)}/${esc(f)}">${esc(f.replace(/\.(md|html)$/, ""))}</a>`).join(" · ") },
    ], "Henüz backtest raporu yok.");
    $("#runs").querySelectorAll("[data-file]").forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault();
      const [run, file] = a.dataset.file.split("/");
      this.show(`/backtests/${encodeURIComponent(run)}/${encodeURIComponent(file)}`, `${run} · ${file}`);
    }));
    $("#data").innerHTML = table(data, [
      { key: "exchange", label: "Borsa" },
      { key: "symbol", label: "Sembol" },
      { key: "timeframe", label: "TF" },
      { key: "bars", label: "Mum", num: true, fmt: (v) => num(v, 0) },
      { key: "start", label: "Başlangıç", fmt: (v) => esc(String(v || "").slice(0, 10)) },
      { key: "end", label: "Bitiş", fmt: (v) => esc(String(v || "").slice(0, 10)) },
    ], "Henüz veri indirilmedi.");
  },
};

// ================================================================== Analiz
const analysis = {
  id: "analysis",
  slug: "analiz",
  title: "Teknik analiz",
  async render(view) {
    view.innerHTML = `<div class="card"><form class="inline" id="af">
        <label class="field">Sembol<input name="symbol" placeholder="BTC/USDT" required></label>
        <button class="btn primary">🔍 Analiz et</button><span class="muted small" id="ast"></span></form>
        <pre class="report" id="asum" hidden></pre></div>
      <div class="grid two" style="grid-template-columns: minmax(240px, 1fr) 3fr">
        <div class="card flush"><h2>Raporlar</h2><div id="alist"></div></div>
        <div class="card" id="aview"><div class="empty">Soldan bir rapor seçin.</div></div>
      </div>`;
    $("#af").addEventListener("submit", async (e) => {
      e.preventDefault();
      const s = String(new FormData(e.target).get("symbol")).trim().toUpperCase();
      $("#ast").innerHTML = '<span class="spinner"></span> analiz ediliyor…';
      try {
        const r = await api(`/api/analyze?symbol=${encodeURIComponent(s.includes("/") ? s : s + "/USDT")}`, { method: "POST" });
        $("#asum").hidden = false;
        $("#asum").textContent = r.summary;
        $("#ast").textContent = "";
        await this.refresh();
        this.open(r.report, r.chart);
      } catch (err) { $("#ast").textContent = ""; toast(err.message, "error"); }
    });
    await this.refresh();
  },
  open(report, chartName) {
    $("#aview").innerHTML = `<div class="row"><h2 style="margin:0">${esc(report.replace(/\.md$/, ""))}</h2><span class="spacer"></span>
      <a class="btn small" href="/reports/${encodeURIComponent(report)}" target="_blank" rel="noopener">Metin</a>
      ${chartName ? `<a class="btn small" href="/reports/${encodeURIComponent(chartName)}" target="_blank" rel="noopener">Grafik (yeni sekme)</a>` : ""}</div>
      ${chartName ? `<iframe class="report" src="/reports/${encodeURIComponent(chartName)}" title="Grafik"></iframe>` : ""}
      <iframe class="report" style="height:480px" src="/reports/${encodeURIComponent(report)}" title="Rapor"></iframe>`;
  },
  async refresh() {
    const list = await api("/api/reports");
    $("#alist").innerHTML = list.length
      ? `<div class="table-wrap"><table><tbody>${list.map((r) => `<tr><td><a href="#" data-r="${esc(r.name)}" data-c="${esc(r.chart || "")}">${esc(r.title)}</a><div class="muted small">${esc(when(new Date(r.modified * 1000).toISOString()))}</div></td></tr>`).join("")}</tbody></table></div>`
      : '<div class="empty">Henüz rapor yok.</div>';
    $("#alist").querySelectorAll("[data-r]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); this.open(a.dataset.r, a.dataset.c || null); }));
  },
};

// ================================================================== Olaylar
const events = {
  id: "events",
  slug: "olaylar",
  title: "Olaylar ve değişiklikler",
  level: "",
  async render(view) {
    view.innerHTML = `<div class="card flush"><div class="row" style="padding:16px 16px 0"><h2 style="margin:0">Risk olayları</h2><span class="spacer"></span>
        <select id="lvl"><option value="">Tümü</option><option value="critical">critical</option><option value="warning">warning</option><option value="info">info</option></select></div>
        <div id="evt"></div></div>
      <div class="card flush"><h2>Config değişiklik günlüğü</h2><div id="chg"></div></div>`;
    $("#lvl").value = this.level;
    $("#lvl").addEventListener("change", (e) => { this.level = e.target.value; this.refresh(); });
    await this.refresh();
    every(20000, () => this.refresh());
  },
  async refresh() {
    const [ev, ch] = await Promise.all([api("/api/events?limit=300"), api("/api/config-changes?limit=300")]);
    $("#evt").innerHTML = eventsTable(this.level ? ev.filter((e) => e.level === this.level) : ev);
    $("#chg").innerHTML = table(ch, [
      { key: "timestamp", label: "Zaman", fmt: when },
      { key: "path", label: "Ayar", fmt: (v) => `<span class="mono small">${esc(v)}</span>` },
      { key: "old", label: "Önceki", wrap: true, fmt: (v) => `<span class="mono small">${esc(v ?? "–")}</span>` },
      { key: "new", label: "Yeni", wrap: true, fmt: (v) => `<span class="mono small">${esc(v ?? "–")}</span>` },
      { key: "source", label: "Kaynak" },
      { key: "operator", label: "Kim" },
    ], "Kayıt yok.");
  },
};

// ================================================================== Ayarlar
function tree(value, key = null, depth = 0) {
  const label = key === null ? "" : `<span class="k">${esc(key)}</span>: `;
  if (value && typeof value === "object") {
    const entries = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
    if (!entries.length) return `<div class="leaf">${label}${Array.isArray(value) ? "[]" : "{}"}</div>`;
    if (Array.isArray(value) && entries.every(([, v]) => v === null || typeof v !== "object"))
      return `<div class="leaf">${label}[${entries.map(([, v]) => esc(JSON.stringify(v))).join(", ")}]</div>`;
    return `<details ${depth < 1 ? "open" : ""}><summary>${label}${Array.isArray(value) ? `[${entries.length}]` : ""}</summary>${entries.map(([k, v]) => tree(v, k, depth + 1)).join("")}</details>`;
  }
  return `<div class="leaf">${label}<span>${esc(JSON.stringify(value))}</span></div>`;
}

const settings = {
  id: "settings",
  slug: "ayarlar",
  title: "Ayarlar",
  async render(view) {
    const c = await api("/api/config");
    view.innerHTML = `<div class="grid two">
        <div class="card"><h2>Çalışma modu</h2>
          <p><span class="pill ${c.sends_real_orders ? "bad" : "info"}">${esc(String(c.mode).toUpperCase())}</span>
          ${c.sends_real_orders ? '<span class="neg">Gerçek emir gönderiliyor.</span>' : '<span class="muted">Gerçek para ile emir yok.</span>'}</p>
          <p class="muted small">Live için .env'de <span class="mono">TRADING_MODE=live</span> <b>ve</b> config.yaml'da <span class="mono">live_trading_confirmed: true</span> birlikte gerekir.</p></div>
        <div class="card"><h2>Değişiklik nasıl yapılır?</h2>
          <p class="small">Risk ve strateji parametreleri güvenlik için panelden düzenlenmez: <span class="mono">config/config.yaml</span> dosyasını değiştirip botu yeniden başlatın. Her değişiklik önceki değeriyle <a href="#/olaylar">config günlüğüne</a> yazılır; testnet/live'da değişen strateji parametreleri önce gölgede çalışır.</p>
          <p class="small">Panelden yapılabilenler: kill switch / devam, strateji aç-kapa (yeniden başlatmada config'e döner), kanarya tavanı, gölge onayı, analiz, backtest, veri indirme.</p></div>
      </div>
      <div class="card"><div class="row"><h2 style="margin:0">Geçerli yapılandırma</h2><span class="spacer"></span><button class="btn small" id="cpy">JSON kopyala</button></div>
        <p class="muted small">API anahtarları ve tokenlar yapılandırmanın parçası değildir; burada gösterilmez.</p>
        <div class="tree">${tree(c.config)}</div></div>`;
    $("#cpy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(JSON.stringify(c.config, null, 2)); toast("Kopyalandı.", "success"); }
      catch { toast("Panoya kopyalanamadı.", "error"); }
    });
  },
};

export const PAGES = [overview, positions, chart, strategies, risk, arbitrage, backtest, analysis, events, settings];
