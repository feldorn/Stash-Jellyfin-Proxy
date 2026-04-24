"use strict";

/* ---- Global app state ---- */
const app = {
  config: {},             // current /api/config payload
  envFields: new Set(),   // keys overridden by env (read-only in UI)
  dirty: new Map(),       // fieldName -> newValue (pending save, per section)
  restartNeeded: false,
  _pageInit: {},          // tabName -> has-been-initialized flag
};

/* ---- Helpers ---- */
const qs  = (sel, root = document) => root.querySelector(sel);
const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function apiGet(path) {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

async function apiPost(path, body) {
  const r = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: body == null ? null : JSON.stringify(body),
  });
  const txt = await r.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch {}
  if (!r.ok) throw new Error((data && data.error) || `${path}: HTTP ${r.status}`);
  return data;
}

function toast(msg, kind = "") {
  const stack = qs("#toast-stack");
  if (!stack) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  stack.appendChild(el);
  if (kind !== "error") {
    setTimeout(() => el.remove(), 3200);
  } else {
    el.addEventListener("click", () => el.remove());
  }
}

function formatUptime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m`;
  return `${m}m ${sec % 60}s`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* ---- Nav routing ---- */
function activatePage(name) {
  qsa(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.page === name));
  qsa(".page").forEach((el) => el.classList.toggle("active", el.dataset.page === name));
  if (!app._pageInit[name] && typeof window[`init_${name}`] === "function") {
    try { window[`init_${name}`](); } catch (e) { console.error(`init_${name} failed`, e); }
    app._pageInit[name] = true;
  }
  if (typeof window[`show_${name}`] === "function") {
    try { window[`show_${name}`](); } catch (e) { console.error(`show_${name} failed`, e); }
  }
  history.replaceState(null, "", `#${name}`);
}

function wireNav() {
  qsa(".nav-item").forEach((el) => {
    el.addEventListener("click", () => activatePage(el.dataset.page));
  });
  const initial = (location.hash || "#dashboard").replace(/^#/, "");
  if (qs(`.nav-item[data-page="${initial}"]`)) {
    activatePage(initial);
  } else {
    activatePage("dashboard");
  }
}

/* ---- Sidebar status poller ---- */
async function pollStatus() {
  try {
    const s = await apiGet("/api/status");
    const proxyDot = qs("#sidebar-proxy-dot");
    const proxyLbl = qs("#sidebar-proxy-label");
    const stashDot = qs("#sidebar-stash-dot");
    const stashLbl = qs("#sidebar-stash-label");
    const upt = qs("#sidebar-uptime");

    proxyDot.className = "status-dot " + (s.running ? "on" : "err");
    proxyLbl.textContent = s.running ? "Proxy Running" : "Proxy Down";

    stashDot.className = "status-dot " + (s.stashConnected ? "ok" : "err");
    stashLbl.textContent = s.stashConnected
      ? (s.stashVersion ? `Stash ${s.stashVersion}` : "Stash OK")
      : "Stash Error";

    upt.textContent = `Uptime: ${formatUptime(s.uptime)}`;
    qs("#brand-version").textContent = s.version || "";
  } catch {
    /* leave prior state on transient failure */
  }
}

/* ---- Restart banner + action ---- */
function markRestartNeeded() {
  app.restartNeeded = true;
  qs("#restart-banner").classList.add("open");
}

async function doRestart() {
  if (!confirm("Restart the proxy now? Active streams will be interrupted.")) return;
  try {
    await apiPost("/api/restart", {});
    toast("Restart initiated — reconnecting…", "success");
    setTimeout(() => window.location.reload(), 3500);
  } catch (e) {
    toast(`Restart failed: ${e.message}`, "error");
  }
}

/* ---- Config cache ---- */
async function loadConfig(force = false) {
  if (!force && Object.keys(app.config).length) return app.config;
  const data = await apiGet("/api/config");
  app.config = data.config || {};
  app.envFields = new Set(data.env_fields || []);
  return app.config;
}

async function saveConfig(patch) {
  const res = await apiPost("/api/config", patch);
  if (res.applied_immediately && res.applied_immediately.length) {
    toast(`Saved. Applied live: ${res.applied_immediately.join(", ")}`, "success");
  } else if (res.needs_restart && res.needs_restart.length) {
    toast(`Saved. Requires restart: ${res.needs_restart.join(", ")}`, "warning");
    markRestartNeeded();
  } else {
    toast("Saved.", "success");
  }
  await loadConfig(true);
  return res;
}

/* ============================================================ */
/* Logs tab                                                     */
/* ============================================================ */
const logsState = {
  entries: [],
  interval: null,
  activeLevels: new Set(["DEBUG", "INFO", "WARNING", "ERROR"]),
  search: "",
};

async function fetchLogs() {
  try {
    const n = parseInt(qs("#log-line-count").value || "250", 10);
    const data = await apiGet(`/api/logs?limit=${n}`);
    logsState.entries = data.entries || [];
    renderLogs();
  } catch (e) {
    qs("#log-viewer").innerHTML = `<em style="color: var(--error);">Failed to load logs: ${escapeHtml(e.message)}</em>`;
  }
}

function renderLogs() {
  const viewer = qs("#log-viewer");
  const filtered = logsState.entries.filter((e) => {
    const lvl = (e.level || "INFO").toUpperCase();
    if (!logsState.activeLevels.has(lvl)) return false;
    if (logsState.search && !(e.message || "").toLowerCase().includes(logsState.search)) return false;
    return true;
  });
  const total = logsState.entries.length;
  const shown = filtered.length;
  qs("#log-count-indicator").textContent = shown === total
    ? `${shown} lines`
    : `Showing ${shown} of ${total} lines`;

  if (!filtered.length) {
    viewer.innerHTML = `<em style="color: var(--text-faint);">No log lines match the current filter.</em>`;
    return;
  }
  const frag = document.createDocumentFragment();
  for (const e of filtered) {
    const line = document.createElement("div");
    line.className = "log-line";
    const lvl = (e.level || "INFO").toUpperCase();
    line.innerHTML =
      `<span class="log-ts">${escapeHtml(e.timestamp || "")}</span>` +
      `<span class="log-lvl ${escapeHtml(lvl)}">${escapeHtml(lvl)}</span>` +
      `<span class="log-msg">${escapeHtml(e.message || "")}</span>`;
    frag.appendChild(line);
  }
  viewer.replaceChildren(frag);
  if (qs("#log-autoscroll").checked) {
    viewer.scrollTop = viewer.scrollHeight;
  }
}

window.init_logs = function () {
  qsa("#log-level-filter .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      chip.classList.toggle("active");
      const level = chip.dataset.level;
      if (chip.classList.contains("active")) logsState.activeLevels.add(level);
      else logsState.activeLevels.delete(level);
      renderLogs();
    });
  });
  qs("#log-search").addEventListener("input", (e) => {
    logsState.search = e.target.value.trim().toLowerCase();
    renderLogs();
  });
  qs("#log-line-count").addEventListener("change", fetchLogs);
  qs("#log-refresh-btn").addEventListener("click", fetchLogs);
  qs("#log-clear-btn").addEventListener("click", () => {
    qs("#log-viewer").innerHTML = "";
  });
  qs("#log-autorefresh").addEventListener("change", (e) => {
    if (e.target.checked) {
      if (!logsState.interval) logsState.interval = setInterval(fetchLogs, 3000);
    } else if (logsState.interval) {
      clearInterval(logsState.interval);
      logsState.interval = null;
    }
  });
  fetchLogs();
  if (qs("#log-autorefresh").checked && !logsState.interval) {
    logsState.interval = setInterval(fetchLogs, 3000);
  }
};

/* ---- Boot ---- */
document.addEventListener("DOMContentLoaded", () => {
  wireNav();
  pollStatus();
  setInterval(pollStatus, 10000);
  qs("#restart-now-btn").addEventListener("click", doRestart);
  loadConfig().catch((e) => toast(`Config load failed: ${e.message}`, "error"));
});
