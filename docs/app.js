/* Dashboard client. Renders the stored report; computes nothing. */

const $ = (sel) => document.querySelector(sel);
const state = { report: null, filter: "ALL" };

const money = (v) =>
  v == null ? "—" : v.toLocaleString("en-US", { style: "currency", currency: "USD" });
const pct = (v, digits = 1) =>
  v == null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`;

/* Two deployment shapes are supported:
   - FastAPI serving the app  -> /api/report/latest
   - static hosting (Pages)   -> ./data/latest.json committed by the scheduler
   Try the API first, fall back to the file, then to the cached copy. */
const SOURCES = ["./api/report/latest", "./data/latest.json"];

async function fetchReport() {
  let lastError;
  for (const url of SOURCES) {
    try {
      const res = await fetch(url, { cache: "no-store" });
      if (res.ok) return await res.json();
      lastError = new Error(`${url} -> ${res.status}`);
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError || new Error("no report source reachable");
}

async function load() {
  try {
    state.report = await fetchReport();
    localStorage.setItem("lastReport", JSON.stringify(state.report));
  } catch (err) {
    // Offline or no run yet — fall back to whatever the last successful load was.
    const cached = localStorage.getItem("lastReport");
    if (!cached) {
      $("#meta").textContent = "No report yet. Run the daily job.";
      $("#cards").innerHTML = `<p class="empty">Nothing stored.<br>Run <code>python run_daily.py</code>.</p>`;
      return;
    }
    state.report = JSON.parse(cached);
    $("#meta").dataset.stale = "1";
  }
  render();
}

function renderHeader() {
  const r = state.report;
  const when = new Date(r.generated_at);
  const bits = [
    r.run_date,
    `updated ${when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`,
  ];
  // A redacted (publicly published) report omits the totals entirely rather
  // than sending zeros, so hide the row instead of rendering a fake "$0.00".
  if (r.portfolio_value != null) bits.push(`portfolio ${money(r.portfolio_value)}`);
  if (r.cash != null) bits.push(`cash ${money(r.cash)}`);
  bits.push(`phase ${r.phase}`);
  if (r.redacted) bits.push("amounts hidden");
  if ($("#meta").dataset.stale) bits.push("cached");

  $("#meta").textContent = bits.join(" · ");

  const counts = { BUY: 0, SELL: 0, WATCH: 0, HOLD: 0 };
  r.tickers.forEach((t) => (counts[t.signal] = (counts[t.signal] || 0) + 1));
  $("#summary").innerHTML = ["BUY", "SELL", "WATCH", "HOLD"]
    .map(
      (s) =>
        `<div class="stat ${s.toLowerCase()}"><b>${counts[s] || 0}</b><span>${s}</span></div>`
    )
    .join("");
}

function confidenceColor(band) {
  return { high: "var(--buy)", medium: "var(--watch)", low: "var(--sell)" }[band] || "var(--hold)";
}

function notesFor(t) {
  const notes = [];
  if (!t.dcf.gate.applicable) {
    notes.push({ warn: true, text: `No DCF — ${t.dcf.gate.reason}. Cannot be a buy or a sell.` });
  }
  if (!t.news.news_available) {
    notes.push({ warn: false, text: `No news — ${t.news.reason}. Sentiment assumed neutral.` });
  }
  if (t.risk.downgraded) {
    notes.push({ warn: true, text: `Risk downgrade from ${t.risk.original_signal}: ${t.risk.breaches.join("; ")}` });
  }
  if (t.risk.position) {
    const p = t.risk.position;
    const term = p.term ? `, ${p.term}-term` : "";
    notes.push({
      warn: false,
      text: `Position ${p.quantity} sh · cost ${money(p.cost_basis_per_share)} · unrealized ${money(
        p.unrealized_pnl
      )} (${pct(p.unrealized_pnl_pct)}${term}). Tax impact not calculated.`,
    });
  }
  (t.decision.flags || []).forEach((f) => {
    if (f.startsWith("spec_fill") || f.startsWith("news_unavailable") || f.startsWith("dcf_not_applicable")) return;
    notes.push({ warn: f.startsWith("risk") || f.startsWith("confidence_gate"), text: f });
  });
  return notes;
}

function card(t) {
  const gap = t.dcf.valuation_gap_pct;
  const gapClass = gap == null ? "" : gap >= 0 ? "pos" : "neg";
  const conf = t.confidence;

  const nums = `
    <div class="nums">
      <div class="num"><span>Price</span><b>${money(t.price)}</b></div>
      <div class="num"><span>Fair value</span><b>${money(t.dcf.fair_value)}</b></div>
      <div class="num"><span>Gap</span><b class="${gapClass}">${pct(gap)}</b></div>
      <div class="num"><span>Sentiment</span><b>${
        t.news.news_available ? t.news.aggregate_label : "n/a"
      }</b></div>
      ${conf ? `<div class="num"><span>Confidence</span><b>${Math.round(conf.value)}</b></div>` : ""}
    </div>`;

  const notes = notesFor(t);
  const notesHtml = notes.length
    ? `<ul class="notes">${notes
        .map((n) => `<li class="${n.warn ? "warn" : ""}">${n.text}</li>`)
        .join("")}</ul>`
    : "";

  const confBar = conf
    ? `<div class="conf-bar"><i style="width:${Math.max(0, Math.min(100, conf.value))}%;background:${confidenceColor(
        conf.band
      )}"></i></div>`
    : "";

  return `
    <article class="card ${t.signal}">
      <div class="card-head">
        <span class="ticker">${t.ticker}</span>
        <span class="tag ${t.signal}">${t.signal}</span>
      </div>
      ${nums}
      <p class="rationale">${t.rationale || ""}</p>
      ${notesHtml}
      ${confBar}
    </article>`;
}

function render() {
  renderHeader();
  const order = { BUY: 0, SELL: 1, WATCH: 2, HOLD: 3 };
  const rows = state.report.tickers
    .filter((t) => state.filter === "ALL" || t.signal === state.filter)
    .sort((a, b) => order[a.signal] - order[b.signal] || a.ticker.localeCompare(b.ticker));

  $("#cards").innerHTML = rows.length
    ? rows.map(card).join("")
    : `<p class="empty">Nothing in this bucket today.</p>`;
}

$("#filters").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip");
  if (!btn) return;
  document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c === btn));
  state.filter = btn.dataset.filter;
  render();
});

/* --- Web Push ---------------------------------------------------------- */

function urlBase64ToUint8Array(base64) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function initPush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;

  const reg = await navigator.serviceWorker.register("./sw.js");
  const existing = await reg.pushManager.getSubscription();
  const btn = $("#push-btn");

  // Static hosting has no API. Subscribing needs a server to POST to, so the
  // button only appears when one is reachable — on Pages you register once
  // against a local server and store the subscription as a repo secret.
  let key = "";
  try {
    const keyRes = await fetch("./api/push/public-key");
    if (keyRes.ok) ({ key } = await keyRes.json());
  } catch (_) {
    return;
  }
  if (!key) return; // VAPID not configured, or no API in this deployment

  if (existing) {
    btn.hidden = true;
    return;
  }

  btn.hidden = false;
  btn.addEventListener("click", async () => {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      btn.textContent = "Alerts blocked";
      return;
    }
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
    await fetch("./api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub.toJSON()),
    });
    btn.textContent = "Alerts on";
    btn.disabled = true;
  });
}

load();
initPush().catch((e) => console.warn("push init failed", e));
