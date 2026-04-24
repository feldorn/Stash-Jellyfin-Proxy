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
/* data-page is set on BOTH the sidebar nav-item and the page section —
   always scope bindings to the section so the form inputs are reachable. */
const pageRoot = (name) => qs(`section.page[data-page="${name}"]`);

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
/* Shared form-binding helpers (used by every config tab)       */
/* ============================================================ */

/* Fill every [data-key="X"] input/select/toggle inside `root` from
   app.config. Call after loadConfig(). */
function bindFormFromConfig(root) {
  const cfg = app.config;
  qsa("[data-key]", root).forEach((el) => {
    const key = el.dataset.key;
    const val = cfg[key];
    if (el.classList.contains("toggle")) {
      setToggleValue(el, !!val);
    } else if (el.tagName === "SELECT") {
      el.value = val == null ? "" : String(val);
    } else if (el.type === "checkbox") {
      el.checked = !!val;
    } else if (el.type === "password") {
      /* Never bind the masked value into a password input — the backend
         returns "********", which would round-trip back on save as the
         literal asterisks. Leaving the field blank (with a "(unchanged)"
         placeholder) means the user must retype to change it; otherwise
         readSectionValues skips the key on save. */
      el.value = "";
    } else if (Array.isArray(val)) {
      el.value = val.join(", ");
    } else {
      el.value = val == null ? "" : String(val);
    }
    if (app.envFields.has(key)) {
      el.setAttribute("disabled", "disabled");
      el.title = "Overridden by environment variable";
    }
  });
  /* Attach dirty-dot tracking once per section. */
  qsa(".card[data-section]", root).forEach((card) => {
    if (card._dirtyBound) return;
    card._dirtyBound = true;
    qsa("[data-key]", card).forEach((el) => {
      const fire = () => {
        const dot = qs(".unsaved-dot", card);
        if (dot) dot.classList.remove("hide");
      };
      if (el.classList.contains("toggle")) {
        el.addEventListener("click", () => {
          el.classList.toggle("on");
          fire();
        });
      } else {
        el.addEventListener("input", fire);
        el.addEventListener("change", fire);
      }
    });
    qsa("[data-save]", card).forEach((btn) => {
      btn.addEventListener("click", () => saveSection(card));
    });
  });
}

function setToggleValue(el, on) {
  el.classList.toggle("on", !!on);
}

function readSectionValues(card) {
  const out = {};
  qsa("[data-key]", card).forEach((el) => {
    if (el.hasAttribute("readonly") || el.hasAttribute("disabled")) return;
    const key = el.dataset.key;
    if (el.classList.contains("toggle")) {
      out[key] = el.classList.contains("on");
    } else if (el.tagName === "SELECT") {
      out[key] = el.value;
    } else if (el.type === "checkbox") {
      out[key] = el.checked;
    } else if (el.type === "password") {
      /* Blank password = user didn't retype = keep existing (the backend
         preserves the stored value when a masked or empty string arrives). */
      if (el.value !== "") out[key] = el.value;
    } else if (el.type === "number") {
      out[key] = el.value === "" ? "" : Number(el.value);
    } else {
      out[key] = el.value;
    }
  });
  return out;
}

async function saveSection(card) {
  const patch = readSectionValues(card);
  try {
    await saveConfig(patch);
    const dot = qs(".unsaved-dot", card);
    if (dot) dot.classList.add("hide");
  } catch (e) {
    toast(`Save failed: ${e.message}`, "error");
  }
}

/* ============================================================ */
/* Connection tab                                               */
/* ============================================================ */
window.init_connection = async function () {
  try { await loadConfig(); } catch {}
  bindFormFromConfig(pageRoot("connection"));

  qs("#conn-test-btn").addEventListener("click", async () => {
    const btn = qs("#conn-test-btn");
    const result = qs("#conn-test-result");
    const root = pageRoot("connection");
    /* Read current form values (may be unsaved edits). Omit blank API key
       so the backend falls back to the stored key. */
    const payload = {
      STASH_URL: qs('[data-key=STASH_URL]', root).value.trim(),
      STASH_GRAPHQL_PATH: qs('[data-key=STASH_GRAPHQL_PATH]', root).value.trim() || "/graphql",
      STASH_VERIFY_TLS: qs('[data-key=STASH_VERIFY_TLS]', root).classList.contains("on"),
    };
    const apiKeyInput = qs('[data-key=STASH_API_KEY]', root).value;
    if (apiKeyInput !== "") payload.STASH_API_KEY = apiKeyInput;

    btn.disabled = true;
    result.textContent = "Testing…";
    result.style.color = "";
    try {
      const res = await apiPost("/api/stash/test", payload);
      if (res.ok) {
        result.innerHTML = `✓ Connected — Stash ${escapeHtml(res.version || "unknown")}`;
        result.style.color = "var(--ok, #36c563)";
      } else {
        result.innerHTML = `✗ Connection failed: ${escapeHtml(res.error || "unknown error")}`;
        result.style.color = "var(--err, #e86464)";
      }
    } catch (e) {
      result.innerHTML = `✗ Connection failed: ${escapeHtml(e.message)}`;
      result.style.color = "var(--err, #e86464)";
    } finally {
      btn.disabled = false;
    }
  });
};

window.show_connection = async function () {
  try { await loadConfig(); bindFormFromConfig(pageRoot("connection")); } catch {}
};

/* ============================================================ */
/* System tab                                                   */
/* ============================================================ */
window.init_system = async function () {
  try { await loadConfig(); } catch {}
  bindFormFromConfig(pageRoot("system"));

  qs("#sys-restart-btn").addEventListener("click", doRestart);

  qs("#sys-clearcache-btn").addEventListener("click", async () => {
    try {
      const res = await apiPost("/api/cache/clear", {});
      toast(`Cache cleared: ${(res.cleared || []).join(", ")}`, "success");
    } catch (e) {
      toast(`Clear cache failed: ${e.message}`, "error");
    }
  });

  qs("#sys-downloadconfig-btn").addEventListener("click", () => {
    window.location.href = "/api/config/download";
  });
};

window.show_system = async function () {
  try { await loadConfig(); bindFormFromConfig(pageRoot("system")); } catch {}
};

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
