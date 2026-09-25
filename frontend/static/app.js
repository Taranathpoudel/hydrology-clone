/* DHM front-end helpers. Talks directly to FastAPI. */
const API = window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost"
  ? "http://127.0.0.1:8000"
  : "http://127.0.0.1:8000";

const $  = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));

const esc = (s) => String(s ?? "")
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");

const fmtNum = (v, unit="") =>
  (v === null || v === undefined || Number.isNaN(v)) ? "–" : `${(+v).toFixed(2)}${unit}`;

const fmtDelay = (m) => {
  if (m === null || m === undefined) return "offline";
  if (m <= 10) return `${m}m`;
  if (m < 60) return `${m}m`;
  if (m < 1440) return `${Math.floor(m/60)}h ${m%60}m`;
  return `${Math.floor(m/1440)}d`;
};

const telemetryBadge = (state) => {
  const map = {
    on_time:"b-on", delayed:"b-warn", critical:"b-crit", offline:"b-off",
  };
  const label = {on_time:"On-time", delayed:"Delayed", critical:"Critical", offline:"Offline"}[state] || state;
  return `<span class="badge ${map[state] || "b-off"}">${label}</span>`;
};

const trendBadge = (t) => {
  if (!t) return `<span class="muted">–</span>`;
  const cls = {RISING:"t-rising", FALLING:"t-falling", STEADY:"t-steady"}[t] || "";
  const arrow = {RISING:"↑", FALLING:"↓", STEADY:"→"}[t] || "";
  return `<span class="trend ${cls}">${t} ${arrow}</span>`;
};

const diffCell = (d) => {
  if (d === null || d === undefined) return `<span class="muted">–</span>`;
  const cls = d >= 0 ? "diff-above" : (d >= -1 ? "diff-near" : "diff-safe");
  const sign = d >= 0 ? "+" : "";
  return `<span class="diff ${cls}">${sign}${d.toFixed(2)}m</span>`;
};

function hazardBlock(hz) {
  const danger = hz.danger || [];
  const warning = hz.warning || [];
  if (danger.length === 0 && warning.length === 0) {
    return `<div class="hz hz-safe">
      <div class="hz-head">
        <div class="hz-title">🟢 No Warning
          <span class="hz-count">0</span></div>
        <span class="muted">All stations below warning levels</span>
      </div>
    </div>`;
  }
  let html = "";
  if (danger.length) {
    html += `<div class="hz hz-danger">
      <div class="hz-head">
        <div class="hz-title">🚨 Danger Level Exceeded
          <span class="hz-count">${danger.length}</span></div>
        <span class="muted">Water level has crossed danger threshold</span>
      </div>
      <div class="chips">
        ${danger.map(s => `
          <span class="chip" title="${esc(s.name)}">
            <b>${esc(s.name)}</b>
            ${diffCell(s.water_level_diff_danger)}
          </span>`).join("")}
      </div>
    </div>`;
  }
  if (warning.length) {
    html += `<div class="hz hz-warning">
      <div class="hz-head">
        <div class="hz-title">⚠️ Warning Level Exceeded
          <span class="hz-count">${warning.length}</span></div>
        <span class="muted">Water level has crossed warning threshold</span>
      </div>
      <div class="chips">
        ${warning.map(s => `
          <span class="chip" title="${esc(s.name)}">
            <b>${esc(s.name)}</b>
            ${diffCell(s.water_level_diff_warning)}
          </span>`).join("")}
      </div>
    </div>`;
  }
  return html;
}

async function jget(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`API ${r.status} on ${path}`);
  return r.json();
}

function setUpdated(iso) {
  const el = $("#updated");
  if (!el) return;
  if (!iso) { el.textContent = "—"; return; }
  const d = new Date(iso);
  el.textContent = "Updated " + d.toLocaleTimeString("en-US", {timeZone:"Asia/Kathmandu"}) + " NPT";
}

/* -------- Dashboard -------- */
async function renderDashboard() {
  try {
    const [s, hz, list] = await Promise.all([
      jget("/api/stats"),
      jget("/api/hazard"),
      jget("/api/stations?state=delayed&sort=delay_desc&limit=25"),
    ]);
    setUpdated(s.updated_at);

    $$("#kpi-grid .kpi-val").forEach(el => {
      const k = el.dataset.k;
      el.textContent = s.stats[k] ?? "–";
    });

    $("#hazard").innerHTML = `<div class="hazard-stack">${hazardBlock(hz)}</div>`;

    const tbody = $("#top-delayed");
    const rows = list.stations || [];
    tbody.innerHTML = rows.length ? rows.map(s => `
      <tr>
        <td><div class="station">${esc(s.name)}</div>
            <div class="muted">${esc(s.district)}</div></td>
        <td>${esc(s.basin)}</td>
        <td class="mono">${fmtNum(s.water_level, "m")}</td>
        <td>${trendBadge(s.trend)}</td>
        <td>${diffCell(s.water_level_diff_warning)}</td>
        <td>${telemetryBadge(s.telemetry_state)} ${fmtDelay(s.delay_minutes)}</td>
      </tr>`).join("") : `<tr><td colspan="6" class="empty">No data</td></tr>`;
  } catch (e) {
    console.error(e);
  }
}

/* -------- River Watch -------- */
async function renderRiverWatch() {
  const q    = $("#q");
  const selT = $("#trend");
  const selS = $("#sort");

  const load = async () => {
    const stateMap = {RISING:"rising", FALLING:"falling", STEADY:"steady", all:"all"};
    const stateKey = stateMap[selT.value] || "all";
    const data = await jget(
      `/api/stations?state=${stateKey}&sort=${selS.value}&q=${encodeURIComponent(q.value)}&limit=2000`
    );
    setUpdated(data.updated_at);
    const rows = data.stations || [];
    $("#rw-table tbody").innerHTML = rows.length ? rows.map((s,i) => `
      <tr>
        <td class="muted">${i+1}</td>
        <td><div class="station">${esc(s.name)}</div>
            <div class="muted">${esc(s.station_index || "")}</div></td>
        <td>${esc(s.basin)}</td>
        <td>${esc(s.district)}</td>
        <td class="mono">${fmtNum(s.water_level,"m")}</td>
        <td class="mono">${fmtNum(s.warning_level,"m")}</td>
        <td class="mono">${fmtNum(s.danger_level,"m")}</td>
        <td>${diffCell(s.water_level_diff_warning)}</td>
        <td>${diffCell(s.water_level_diff_danger)}</td>
        <td>${trendBadge(s.trend)}</td>
        <td>${telemetryBadge(s.telemetry_state)}</td>
      </tr>`).join("") : `<tr><td colspan="11" class="empty">No matching stations</td></tr>`;
  };

  ["input","change"].forEach(ev => {
    q.addEventListener(ev, debounce(load, 200));
    selT.addEventListener(ev, load);
    selS.addEventListener(ev, load);
  });
  await load();
}

/* -------- Data Watch -------- */
async function renderDataWatch() {
  const q = $("#q"), stateSel = $("#state"), sortSel = $("#sort");

  const load = async () => {
    const data = await jget(
      `/api/stations?state=${stateSel.value}&sort=${sortSel.value}&q=${encodeURIComponent(q.value)}&limit=2000`
    );
    setUpdated(data.updated_at);
    const rows = data.stations || [];
    $("#dw-table tbody").innerHTML = rows.length ? rows.map((s,i) => `
      <tr>
        <td class="muted">${i+1}</td>
        <td><div class="station">${esc(s.name)}</div>
            <div class="muted">${esc(s.nepali_name || "")}</div></td>
        <td>${esc(s.basin)}</td>
        <td class="mono">${esc(s.station_index || "–")}</td>
        <td>${telemetryBadge(s.telemetry_state)}</td>
        <td class="mono">${s.latest_time ? new Date(s.latest_time).toLocaleString("en-US",{timeZone:"Asia/Kathmandu"}) : "–"}</td>
        <td>${fmtDelay(s.delay_minutes)}</td>
      </tr>`).join("") : `<tr><td colspan="7" class="empty">No matching stations</td></tr>`;
  };

  ["input","change"].forEach(ev => {
    q.addEventListener(ev, debounce(load, 200));
    stateSel.addEventListener(ev, load);
    sortSel.addEventListener(ev, load);
  });
  await load();
}

/* -------- Compare -------- */
async function renderCompare() {
  const nearI = $("#near"), riseI = $("#rising");
  const load = async () => {
    const n = Math.max(1, Math.min(20, parseInt(nearI.value,10) || 5));
    const r = Math.max(1, Math.min(20, parseInt(riseI.value,10) || 5));
    const data = await jget(`/api/compare?near=${n}&rising=${r}`);
    setUpdated(data.updated_at);
    const nw = data.near_warning || [], ri = data.rising || [];
    const max = Math.max(nw.length, ri.length, 1);
    const cell = (s) => {
      if (!s) return `<td colspan="4" class="empty">—</td>`;
      return `
        <td><div class="station">${esc(s.name)}</div>
            <div class="muted">${esc(s.basin)} · ${esc(s.district)}</div></td>
        <td class="mono">${fmtNum(s.water_level,"m")}</td>
        <td>${diffCell(s.water_level_diff_warning)}</td>
        <td>${trendBadge(s.trend)}</td>`;
    };
    let html = "";
    for (let i = 0; i < max; i++) {
      html += `<tr>${cell(nw[i])}<td class="divider"></td>${cell(ri[i])}</tr>`;
    }
    $("#cmp-body").innerHTML = html;
  };
  $("#reload").addEventListener("click", load);
  await load();
}

/* -------- util -------- */
function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

window.DHM = { renderDashboard, renderRiverWatch, renderDataWatch, renderCompare };