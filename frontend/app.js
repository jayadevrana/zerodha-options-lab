/* Options Lab frontend — chain, leg builder, payoff chart, analytics. */
"use strict";

const state = {
  market: null,        // /api/market
  chain: null,         // /api/chain for selected expiry
  expiry: null,
  legs: [],            // {kind, strike, expiry, side, lots, lot_size, entry_price, iv, tradingsymbol}
  analysis: null,
  margin: null,
  evalFrac: 0,         // 0 = now, 1 = expiry
  ivShift: 0,
  connected: false,
};

const $ = (id) => document.getElementById(id);
const fmt = (n, d = 2) => n == null ? "—" :
  Number(n).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: 0 });
const rup = (n) => n == null ? "—" : (n < 0 ? "-₹" : "₹") + fmt(Math.abs(n), 0);

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* keep statusText */ }
    throw new Error(msg);
  }
  return r.json();
}
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ---------------- session / market ---------------- */
async function refreshSession() {
  try {
    const s = await api("/api/session");
    state.connected = s.connected;
    $("connDot").className = "dot " + (s.connected ? "on" : "off");
    $("connText").textContent = s.connected ? s.user_id : "offline";
    $("connectBtn").style.display = s.connected ? "none" : "";
  } catch (e) { /* server down; leave UI as-is */ }
}

async function connect() {
  $("connectBtn").disabled = true; $("connText").textContent = "logging in…";
  try {
    const s = await post("/api/login", {});
    if (!s.connected) alert("Login failed: " + (s.error || "unknown"));
  } catch (e) { alert("Login failed: " + e.message); }
  $("connectBtn").disabled = false;
  await refreshSession();
  if (state.connected) { await refreshMarket(); await loadChain(); }
}

async function refreshMarket() {
  if (!state.connected) return;
  try {
    const m = await api("/api/market");
    state.market = m;
    $("spot").textContent = fmt(m.spot);
    const chg = m.spot - (m.spot_ohlc?.close || m.spot);
    $("spotChg").textContent = (chg >= 0 ? "+" : "") + fmt(chg) +
      " (" + fmt(100 * chg / (m.spot_ohlc?.close || m.spot)) + "%)";
    $("spotChg").className = chg >= 0 ? "pos" : "neg";
    $("vix").textContent = fmt(m.vix);
    $("cash").textContent = rup(m.cash);
    const sel = $("expirySel");
    if (sel.options.length === 0 && m.expiries?.length) {
      m.expiries.forEach(e => sel.add(new Option(e, e)));
      state.expiry = m.expiries[0];
      sel.value = state.expiry;
    }
  } catch (e) { console.warn("market:", e.message); }
}

/* ---------------- option chain ---------------- */
async function loadChain() {
  if (!state.connected || !state.expiry) return;
  try {
    const c = await api(`/api/chain?expiry=${state.expiry}`);
    state.chain = c;
    $("dte").textContent = c.dte;
    $("atmIv").textContent = c.atm_iv ? c.atm_iv + "%" : "—";
    $("chainTs").textContent = "· " + (c.ts || "").slice(11, 19);
    renderChain();
  } catch (e) { console.warn("chain:", e.message); }
}

function renderChain() {
  const c = state.chain; if (!c) return;
  const maxOi = Math.max(1, ...c.rows.flatMap(r =>
    [r.CE?.oi || 0, r.PE?.oi || 0]));
  const body = $("chainBody");
  body.innerHTML = "";
  for (const row of c.rows) {
    const div = document.createElement("div");
    div.className = "crow" + (row.strike === c.atm ? " atm" : "");
    const ce = row.CE || {}, pe = row.PE || {};
    div.innerHTML = `
      <span class="oi ce"><i class="bar" style="width:${100 * (ce.oi || 0) / maxOi}%"></i><span>${fmtOi(ce.oi)}</span></span>
      <span class="iv">${ce.iv ?? ""}</span>
      <span>${fmt(ce.ltp)}</span>
      <span class="bs">
        <button class="b" data-k="CE" data-s="${row.strike}" data-d="1">B</button>
        <button class="s" data-k="CE" data-s="${row.strike}" data-d="-1">S</button></span>
      <span class="strike">${fmt(row.strike, 0)}</span>
      <span class="bs">
        <button class="b" data-k="PE" data-s="${row.strike}" data-d="1">B</button>
        <button class="s" data-k="PE" data-s="${row.strike}" data-d="-1">S</button></span>
      <span>${fmt(pe.ltp)}</span>
      <span class="iv">${pe.iv ?? ""}</span>
      <span class="oi pe"><i class="bar" style="width:${100 * (pe.oi || 0) / maxOi}%"></i><span>${fmtOi(pe.oi)}</span></span>`;
    body.appendChild(div);
  }
  body.querySelectorAll(".bs button").forEach(b =>
    b.addEventListener("click", () =>
      addLegFromChain(b.dataset.k, +b.dataset.s, +b.dataset.d)));
  // scroll ATM into view on first render
  const atmEl = body.querySelector(".crow.atm");
  if (atmEl && !body.dataset.scrolled) {
    atmEl.scrollIntoView({ block: "center" }); body.dataset.scrolled = "1";
  }
}
const fmtOi = (oi) => !oi ? "" : oi >= 1e5 ? (oi / 1e5).toFixed(1) + "L" : (oi / 1e3).toFixed(0) + "K";

/* ---------------- legs ---------------- */
function chainCell(strike, kind) {
  const row = (state.chain?.rows || []).find(r => r.strike === strike);
  return row ? row[kind] : null;
}

function addLegFromChain(kind, strike, side) {
  const cell = chainCell(strike, kind);
  if (!cell) return;
  state.legs.push({
    kind, strike, expiry: state.expiry, side,
    lots: 1, lot_size: cell.lot_size || state.chain.lot_size,
    entry_price: cell.mid || cell.ltp, iv: cell.iv,
    tradingsymbol: cell.tradingsymbol,
  });
  renderLegs(); analyzeSoon(); marginSoon();
}

function renderLegs() {
  const tb = $("legsBody");
  tb.innerHTML = "";
  if (!state.legs.length) {
    tb.innerHTML = `<tr class="empty"><td colspan="6">Click B / S in the chain, or pick a template above</td></tr>`;
    clearOutputs(); return;
  }
  state.legs.forEach((l, i) => {
    const tr = document.createElement("tr");
    const name = l.kind === "FUT" ? "NIFTY FUT" : `${fmt(l.strike, 0)} ${l.kind}`;
    tr.innerHTML = `
      <td><button class="side-toggle ${l.side > 0 ? "buy" : "sell"}" data-i="${i}">${l.side > 0 ? "B" : "S"}</button></td>
      <td>${name}<div class="muted">${l.expiry}</div></td>
      <td><input class="lots" data-i="${i}" data-f="lots" type="number" min="1" value="${l.lots}"></td>
      <td><input data-i="${i}" data-f="entry_price" type="number" step="0.05" value="${l.entry_price}"></td>
      <td><input data-i="${i}" data-f="iv" type="number" step="0.1" value="${l.iv ?? ""}"></td>
      <td><button class="del" data-i="${i}">×</button></td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll(".side-toggle").forEach(b => b.addEventListener("click", () => {
    state.legs[+b.dataset.i].side *= -1; renderLegs(); analyzeSoon(); marginSoon();
  }));
  tb.querySelectorAll("input").forEach(inp => inp.addEventListener("change", () => {
    const l = state.legs[+inp.dataset.i];
    l[inp.dataset.f] = +inp.value || (inp.dataset.f === "lots" ? 1 : 0);
    if (inp.dataset.f === "lots") l.lots = Math.max(1, Math.round(l.lots));
    analyzeSoon(); marginSoon();
  }));
  tb.querySelectorAll(".del").forEach(b => b.addEventListener("click", () => {
    state.legs.splice(+b.dataset.i, 1); renderLegs(); analyzeSoon(); marginSoon();
  }));
}

/* ---------------- templates ---------------- */
const TEMPLATES = [
  ["Buy Call", (a) => [L("CE", a, 1)]],
  ["Buy Put", (a) => [L("PE", a, 1)]],
  ["Sell Straddle", (a) => [L("CE", a, -1), L("PE", a, -1)]],
  ["Buy Straddle", (a) => [L("CE", a, 1), L("PE", a, 1)]],
  ["Sell Strangle", (a) => [L("CE", a + 300, -1), L("PE", a - 300, -1)]],
  ["Iron Condor", (a) => [L("PE", a - 500, 1), L("PE", a - 300, -1), L("CE", a + 300, -1), L("CE", a + 500, 1)]],
  ["Iron Fly", (a) => [L("PE", a - 400, 1), L("PE", a, -1), L("CE", a, -1), L("CE", a + 400, 1)]],
  ["Bull Call Spread", (a) => [L("CE", a, 1), L("CE", a + 200, -1)]],
  ["Bear Put Spread", (a) => [L("PE", a, 1), L("PE", a - 200, -1)]],
  ["Bull Put Spread", (a) => [L("PE", a - 100, -1), L("PE", a - 300, 1)]],
  ["Bear Call Spread", (a) => [L("CE", a + 100, -1), L("CE", a + 300, 1)]],
  ["Call Butterfly", (a) => [L("CE", a - 200, 1), L("CE", a, -2), L("CE", a + 200, 1)]],
];
function L(kind, strike, side) { return { kind, strike, side }; }

function nearestStrike(target) {
  const ks = (state.chain?.rows || []).map(r => r.strike);
  if (!ks.length) return target;
  return ks.reduce((b, k) => Math.abs(k - target) < Math.abs(b - target) ? k : b);
}

function applyTemplate(build) {
  if (!state.chain) { alert("Load the chain first (Connect)."); return; }
  const atm = state.chain.atm;
  state.legs = [];
  for (const spec of build(atm)) {
    const strike = nearestStrike(spec.strike);
    const cell = chainCell(strike, spec.kind);
    if (!cell) continue;
    const lots = Math.abs(spec.side);
    state.legs.push({
      kind: spec.kind, strike, expiry: state.expiry,
      side: spec.side > 0 ? 1 : -1, lots,
      lot_size: cell.lot_size || state.chain.lot_size,
      entry_price: cell.mid || cell.ltp, iv: cell.iv,
      tradingsymbol: cell.tradingsymbol,
    });
  }
  renderLegs(); analyzeSoon(); marginSoon();
}

function renderTemplates() {
  const box = $("templates");
  TEMPLATES.forEach(([name, build]) => {
    const b = document.createElement("button");
    b.textContent = name;
    b.addEventListener("click", () => applyTemplate(build));
    box.appendChild(b);
  });
}

/* ---------------- analysis ---------------- */
function evalDateISO() {
  if (!state.legs.length) return null;
  const exp = state.legs.map(l => l.expiry).sort()[0];
  const end = new Date(exp + "T15:30:00+05:30").getTime();
  const now = Date.now();
  const t = now + (end - now) * state.evalFrac;
  return new Date(t).toISOString();
}

async function analyze() {
  if (!state.legs.length || !state.chain) return;
  const evalIso = evalDateISO();
  $("evalLabel").textContent = state.evalFrac === 0 ? "now" :
    new Date(evalIso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  try {
    state.analysis = await post("/api/analyze", {
      legs: state.legs, spot: state.chain.spot,
      ref_iv: state.chain.atm_iv, eval_date: evalIso, iv_shift: state.ivShift,
      smile: state.chain.smile || null,
    });
    renderMetrics(); renderChart(); renderGreeks(); renderPnlTable(); renderSd();
  } catch (e) { console.warn("analyze:", e.message); }
}
const analyzeSoon = debounce(analyze, 250);

async function refreshMargin() {
  if (!state.legs.length || !state.connected) { state.margin = null; return; }
  try {
    state.margin = await post("/api/margin", { legs: state.legs, spot: state.chain?.spot || 0 });
  } catch (e) { state.margin = { total: null }; }
  renderMetrics(); renderPnlTable();
}
const marginSoon = debounce(refreshMargin, 1200);

function card(label, value, cls = "") {
  return `<div class="card"><label>${label}</label><b class="${cls}">${value}</b></div>`;
}

function renderMetrics() {
  const a = state.analysis; if (!a) return;
  const mp = a.max_profit_unlimited ? "Unlimited" : rup(a.max_profit);
  const ml = a.max_loss_unlimited ? "Unlimited" : rup(a.max_loss);
  const prem = a.net_premium;
  const sm = a.smile_metrics && !a.smile_metrics.error ? a.smile_metrics : null;
  const popMain = sm ? sm.gross.pop : a.pop;
  const evNet = sm?.net ? sm.net.ev : null;
  let html =
    card("POP " + (sm ? "(smile)" : "(lognormal)"), popMain + "%",
      popMain >= 50 ? "green" : "amber") +
    card("Max profit", mp, "green") +
    card("Max loss", ml, "red") +
    card("Reward / Risk", a.reward_risk ?? "—") +
    card("Net " + (prem >= 0 ? "credit" : "debit"), rup(Math.abs(prem)), prem >= 0 ? "green" : "red") +
    card("Breakevens", (a.breakevens || []).map(b => fmt(b, 0)).join("  /  ") || "—") +
    (sm
      ? card("EV net of costs", evNet != null ? rup(evNet) : "—",
          (evNet ?? -1) >= 0 ? "green" : "red") +
        card("CVaR 95 (tail loss)", sm.gross.cvar95 != null ? rup(-Math.abs(sm.gross.cvar95)) : "—", "red") +
        card("POP net / lognormal", (sm.net ? sm.net.pop + "%" : "—") + " / " + a.pop + "%")
      : card("Expected value", rup(a.expected_value), a.expected_value >= 0 ? "green" : "red")) +
    card("Margin (final)", state.margin?.total != null ? rup(state.margin.total) : "—") +
    card("Hedge benefit", state.margin?.hedge_benefit != null ? rup(state.margin.hedge_benefit) : "—") +
    card("Entry costs", a.costs ? rup(a.costs.entry) : "—") +
    card("Forward / basis", a.forward ? fmt(a.forward, 0) +
      (state.chain?.smile?.basis != null ? " (" + (state.chain.smile.basis >= 0 ? "+" : "") + fmt(state.chain.smile.basis, 1) + ")" : "") : "—") +
    card("DTE (var-time)", a.dte) +
    card("Ref IV", a.ref_iv_pct + "%" +
      (sm ? " · fit " + sm.model + (sm.rmse_volpts != null ? " ±" + sm.rmse_volpts : "") : ""));
  if (a.multi_expiry) html += card("Note", "multi-expiry: POP/expiry curve approximate", "amber");
  $("metrics").innerHTML = html;
}

function renderGreeks() {
  const g = state.analysis?.greeks; if (!g) return;
  $("greeks").innerHTML =
    card("Delta", fmt(g.delta, 1)) + card("Gamma", fmt(g.gamma, 3)) +
    card("Theta / day", rup(g.theta), g.theta >= 0 ? "green" : "red") +
    card("Vega / 1%", rup(g.vega), g.vega >= 0 ? "green" : "red") +
    card("Rho", fmt(g.rho, 1));
}

function renderSd() {
  const sd = state.analysis?.sd_bands; if (!sd) return;
  $("sdRow").innerHTML = [1, 2, 3].map(n => {
    const b = sd[String(n)];
    return `<b>${n}σ</b> ±${fmt(b.points, 0)} pts (${b.pct}%) → ${fmt(b.low, 0)} – ${fmt(b.high, 0)}`;
  }).join(" &nbsp;|&nbsp; ");
}

/* ---------------- chart ---------------- */
let chart;
function renderChart() {
  const a = state.analysis; if (!a) return;
  if (!chart) chart = echarts.init($("payoffChart"), null, { renderer: "canvas" });
  const spot = state.chain.spot;
  const sd = a.sd_bands["1"], sd2 = a.sd_bands["2"];
  const data = a.grid.map((x, i) => [x, a.expiry_pnl[i]]);
  const dataT0 = a.grid.map((x, i) => [x, a.t0_pnl[i]]);
  chart.setOption({
    animation: false,
    grid: { left: 70, right: 20, top: 20, bottom: 40 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#1a212c", borderColor: "#262f3d", textStyle: { color: "#dfe6ef" },
      valueFormatter: (v) => rup(v),
    },
    xAxis: {
      type: "value", min: a.grid[0], max: a.grid[a.grid.length - 1], scale: true,
      axisLabel: { color: "#7d8896", formatter: (v) => fmt(v, 0) },
      splitLine: { lineStyle: { color: "rgba(255,255,255,.04)" } },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: "#7d8896", formatter: (v) => (v / 1000).toFixed(0) + "k" },
      splitLine: { lineStyle: { color: "rgba(255,255,255,.06)" } },
    },
    visualMap: {
      show: false, seriesIndex: 0, pieces: [
        { gt: 0, color: "#26c281" }, { lte: 0, color: "#f45b69" }],
      dimension: 1,
    },
    series: [
      {
        name: "At expiry", type: "line", data, showSymbol: false, lineStyle: { width: 2 },
        areaStyle: { opacity: 0.12 },
        markLine: {
          symbol: "none", label: { color: "#7d8896" },
          data: [
            { xAxis: spot, label: { formatter: "spot " + fmt(spot, 0) }, lineStyle: { color: "#4f8ef7", type: "solid" } },
            ...(a.breakevens || []).map(b => ({
              xAxis: b, label: { formatter: "BE " + fmt(b, 0) }, lineStyle: { color: "#f5a623", type: "dashed" },
            })),
            { yAxis: 0, lineStyle: { color: "rgba(255,255,255,.25)" }, label: { show: false } },
          ],
        },
        markArea: {
          silent: true,
          data: [
            [{ xAxis: sd.low, itemStyle: { color: "rgba(79,142,247,.07)" } }, { xAxis: sd.high }],
            [{ xAxis: sd2.low, itemStyle: { color: "rgba(79,142,247,.04)" } }, { xAxis: sd2.high }],
          ],
        },
      },
      {
        name: "Eval date", type: "line", data: dataT0, showSymbol: false,
        lineStyle: { width: 2, type: "dashed", color: "#f5a623" },
      },
    ],
  });
}

/* ---------------- P&L table ---------------- */
function renderPnlTable() {
  const a = state.analysis; if (!a) return;
  const spot = state.chain.spot;
  const step = 100;
  const base = Math.round(spot / step) * step;
  const marginTotal = state.margin?.total;
  const rows = [];
  for (let k = -6; k <= 6; k++) {
    const s = base + k * step;
    // interpolate from grid
    const pnl = interp(a.grid, a.expiry_pnl, s);
    const ret = marginTotal ? (100 * pnl / marginTotal).toFixed(1) + "%" : "—";
    rows.push(`<tr class="${Math.abs(s - spot) < step / 2 ? "spot-row" : ""}">
      <td>${fmt(s, 0)}</td><td class="${pnl >= 0 ? "pos" : "neg"}">${rup(pnl)}</td><td>${ret}</td></tr>`);
  }
  $("pnlBody").innerHTML = rows.join("");
}

function interp(xs, ys, x) {
  if (x <= xs[0]) return ys[0];
  for (let i = 1; i < xs.length; i++) {
    if (x <= xs[i]) {
      const t = (x - xs[i - 1]) / (xs[i] - xs[i - 1]);
      return ys[i - 1] + t * (ys[i] - ys[i - 1]);
    }
  }
  return ys[ys.length - 1];
}

function clearOutputs() {
  $("metrics").innerHTML = ""; $("greeks").innerHTML = "";
  $("pnlBody").innerHTML = ""; $("sdRow").innerHTML = "";
  if (chart) chart.clear();
}

/* ---------------- execution ---------------- */
function openModal() {
  if (!state.legs.length) return;
  $("modalLegs").innerHTML = state.legs.map(l =>
    `<div class="mleg"><span>${l.side > 0 ? "BUY" : "SELL"} ${l.lots} lot × ${l.tradingsymbol || (l.strike + " " + l.kind)}</span>
     <span>~${fmt(l.entry_price)}</span></div>`).join("");
  $("confirmInput").value = ""; $("modalGo").disabled = true; $("modalResult").textContent = "";
  $("modal").classList.remove("hidden");
}

async function executeBasket() {
  $("modalGo").disabled = true;
  $("modalResult").textContent = "Placing orders…";
  try {
    const r = await post("/api/execute", { legs: state.legs, spot: state.chain?.spot || 0, confirm: "EXECUTE" });
    $("modalResult").textContent = r.orders.map(o =>
      `${o.side} ${o.tradingsymbol}: ${o.status}${o.order_id ? " (" + o.order_id + ")" : ""}`).join("\n");
  } catch (e) {
    $("modalResult").textContent = "Failed: " + e.message;
    $("modalGo").disabled = false;
  }
}

/* ---------------- generator ---------------- */
let genData = null;

async function runGenerator() {
  if (!state.expiry) { alert("Connect and pick an expiry first."); return; }
  const btn = $("genBtn");
  btn.disabled = true;
  $("genStatus").textContent = "Enumerating and scoring combinations…";
  $("genResults").classList.add("hiddenx");
  $("adviceBox").classList.add("hiddenx");
  try {
    genData = await post("/api/generate", {
      expiry: state.expiry,
      capital: +$("gCapital").value || 1000000,
      risk_budget: +$("gRisk").value || 6000,
      view_points: +$("gView").value || 0,
      vrp_ratio: +$("gVrp").value || 0.9,
      min_pop: +$("gPop").value || 0,
      min_rr: +$("gRr").value || 0,
      max_legs: +$("gLegs").value || 6,
      lots_cap: +$("gLots").value || 20,
      band_points: +$("gBand").value || 800,
      top_n: +$("gTop").value || 100,
      allow_naked: $("gNaked").checked,
      otm_only: $("gOtm").checked,
      pop_weight: +$("gPopW").value,
      slippage: +$("gSlip").value || 0.35,
      max_spread_pct: +$("gSpread").value || 6,
      min_oi: +$("gOi").value || 0,
    });
    const d = genData.diagnostics;
    $("genStatus").innerHTML =
      `Universe <b>${d.universe}</b> instruments · enumerated <b>${d.enumerated.toLocaleString()}</b>` +
      ` · passed constraints <b>${d.survivors.toLocaleString()}</b> · showing top <b>${genData.candidates.length}</b>` +
      ` · fwd ${fmt(d.forward, 0)} · VRP ${d.vrp_ratio} · view ${d.view_points > 0 ? "+" : ""}${d.view_points} pts` +
      ` · fit ${d.fit.model}${d.fit.rmse != null ? " ±" + d.fit.rmse : ""}`;
    renderGenTable();
    $("genResults").classList.remove("hiddenx");
  } catch (e) {
    $("genStatus").textContent = "Generator failed: " + e.message;
  }
  btn.disabled = false;
}

function renderGenTable() {
  const tb = $("genBody");
  tb.innerHTML = "";
  for (const c of genData.candidates) {
    const tr = document.createElement("tr");
    if (c.rank === 1) tr.className = "top1";
    const legsHtml = c.legs.map(l =>
      `<span class="legchip ${l.side > 0 ? "b" : "s"}">${l.side > 0 ? "B" : "S"}${l.lots}× ${fmt(l.strike, 0)}${l.kind}</span>`).join("");
    tr.innerHTML = `
      <td>${c.rank}</td>
      <td>${c.family}${c.unlimited ? ' <span class="neg">∞risk</span>' : ""}</td>
      <td>${legsHtml}</td>
      <td><b>${c.score}</b></td>
      <td class="${c.ev >= 0 ? "pos" : "neg"}">${fmt(c.ev, 0)}</td>
      <td>${c.pop}%</td>
      <td class="neg">${fmt(c.cvar95, 0)}</td>
      <td class="pos">${fmt(c.max_profit, 0)}</td>
      <td class="neg">${fmt(c.max_loss, 0)}</td>
      <td>${c.reward_risk ?? "—"}</td>
      <td>${fmt(c.margin_exact ?? c.margin_est, 0)}${c.margin_exact ? "" : "~"}</td>
      <td>${genData.claude_available ? `<button class="askBtn" data-r="${c.rank}">Claude</button>` : ""}</td>`;
    tr.addEventListener("click", (ev) => {
      if (ev.target.classList.contains("askBtn")) return;
      loadCandidate(c);
    });
    tb.appendChild(tr);
  }
  tb.querySelectorAll(".askBtn").forEach(b => b.addEventListener("click", () =>
    askClaude(genData.candidates.find(x => x.rank === +b.dataset.r))));
}

function loadCandidate(c) {
  state.legs = c.legs.map(l => ({ ...l }));
  renderLegs(); analyzeSoon(); marginSoon();
  document.getElementById("legsTable").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function askClaude(c) {
  $("adviceBox").classList.remove("hiddenx");
  $("adviceText").textContent = "Asking Claude (subscription CLI)… this can take up to a minute.";
  try {
    const r = await post("/api/advise", {
      candidate: c,
      market: {
        spot: state.chain?.spot, forward: genData?.smile?.forward,
        atm_iv: genData?.atm_iv, vix: state.market?.vix,
        expiry: state.expiry, dte_var: state.chain?.dte_var,
        view_points: +$("gView").value || 0, vrp_ratio: +$("gVrp").value || 0.9,
      },
    });
    $("adviceText").textContent = `#${c.rank} ${c.family}\n\n${r.advice}`;
  } catch (e) {
    $("adviceText").textContent = "Advisor failed: " + e.message;
  }
}

/* ---------------- wiring ---------------- */
function wire() {
  $("connectBtn").addEventListener("click", connect);
  $("genBtn").addEventListener("click", runGenerator);
  $("expirySel").addEventListener("change", async (e) => {
    state.expiry = e.target.value;
    // re-point legs at new expiry only if user has none; existing legs keep theirs
    await loadChain(); analyzeSoon();
  });
  $("clearLegs").addEventListener("click", () => { state.legs = []; renderLegs(); });
  $("dateSlider").addEventListener("input", (e) => { state.evalFrac = +e.target.value / 100; analyzeSoon(); });
  $("ivSlider").addEventListener("input", (e) => {
    state.ivShift = +e.target.value; $("ivShiftLabel").textContent = e.target.value; analyzeSoon();
  });
  $("executeBtn").addEventListener("click", openModal);
  $("modalCancel").addEventListener("click", () => $("modal").classList.add("hidden"));
  $("confirmInput").addEventListener("input", (e) => {
    $("modalGo").disabled = e.target.value.trim() !== "EXECUTE";
  });
  $("modalGo").addEventListener("click", executeBasket);
  window.addEventListener("resize", () => chart && chart.resize());
}

async function boot() {
  wire(); renderTemplates();
  await refreshSession();
  if (state.connected) { await refreshMarket(); await loadChain(); }
  setInterval(refreshSession, 30000);
  setInterval(refreshMarket, 5000);
  setInterval(async () => { await loadChain(); if (state.legs.length) analyzeSoon(); }, 15000);
}
boot();
