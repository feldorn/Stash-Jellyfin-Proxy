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
    if (el.hasAttribute("data-radio")) {
      /* Radio group container: [data-key] on the wrapper, type=radio
         inputs inside. Select the input whose value matches. */
      qsa("input[type=radio]", el).forEach((r) => {
        r.checked = String(r.value) === (val == null ? "" : String(val));
      });
    } else if (el.classList.contains("toggle")) {
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
    } else if (el.tagName === "TEXTAREA" && el.hasAttribute("data-lines")) {
      /* Stored comma-separated in config; one entry per line in the UI.
         Leading/trailing whitespace stripped per entry. */
      const parts = Array.isArray(val) ? val : (val == null ? "" : String(val)).split(",");
      el.value = parts.map((s) => String(s).trim()).filter(Boolean).join("\n");
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
      if (el.hasAttribute("data-radio")) {
        qsa("input[type=radio]", el).forEach((r) => r.addEventListener("change", fire));
      } else if (el.classList.contains("toggle")) {
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
    if (el.hasAttribute("data-radio")) {
      const checked = qs("input[type=radio]:checked", el);
      if (checked) out[key] = checked.value;
    } else if (el.classList.contains("toggle")) {
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
    } else if (el.tagName === "TEXTAREA" && el.hasAttribute("data-lines")) {
      /* One entry per line; comma-join for the config file. */
      out[key] = el.value.split("\n").map((s) => s.trim()).filter(Boolean).join(", ");
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
/* Dashboard tab                                                */
/* ============================================================ */
const dashState = {
  statsInterval: null,
  streamsInterval: null,
  logsInterval: null,
};

function dotSpan(cls) {
  return `<span class="status-dot ${cls}" style="display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px;"></span>`;
}

async function renderDashStatus() {
  try {
    const s = await apiGet("/api/status");
    qs("#dash-proxy-status").innerHTML = s.running
      ? `${dotSpan("on")}Running`
      : `${dotSpan("err")}Stopped`;
    qs("#dash-proxy-uptime").textContent = `Uptime: ${formatUptime(s.uptime)}`;

    qs("#dash-stash-status").innerHTML = s.stashConnected
      ? `${dotSpan("ok")}Connected`
      : `${dotSpan("err")}Error`;
    qs("#dash-stash-version").textContent = s.stashVersion || "";

    // Migration banner
    const banner = qs("#dash-migration-banner");
    if (s.migrationPerformed && !banner._dismissed) {
      banner.classList.remove("hide");
    }
  } catch {}
}

async function renderDashLibraryAndUsage() {
  try {
    const s = await apiGet("/api/stats");
    qs("#dash-scene-count").textContent = (s.stash.scenes || 0).toLocaleString();
    qs("#dash-lib-scenes").textContent = (s.stash.scenes || 0).toLocaleString();
    qs("#dash-lib-performers").textContent = (s.stash.performers || 0).toLocaleString();
    qs("#dash-lib-studios").textContent = (s.stash.studios || 0).toLocaleString();
    qs("#dash-lib-groups").textContent = (s.stash.groups || 0).toLocaleString();
    qs("#dash-lib-tags").textContent = (s.stash.tags || 0).toLocaleString();

    qs("#dash-streams-today").textContent = s.proxy.streams_today || 0;
    qs("#dash-streams-total").textContent = s.proxy.total_streams || 0;
    qs("#dash-auth-ok").textContent = s.proxy.auth_success || 0;
    qs("#dash-auth-fail").textContent = s.proxy.auth_failed || 0;

    const top = qs("#dash-top-played");
    const items = s.proxy.top_played || [];
    if (!items.length) {
      top.innerHTML = `<div class="field-help">No plays recorded yet.</div>`;
    } else {
      top.innerHTML = items.map((it, i) => `
        <div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border);">
          <div style="flex: 1; min-width: 0;">
            <div style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
              <span style="color: var(--text-dim); margin-right: 8px;">${i + 1}.</span>${escapeHtml(it.title || it.scene_id)}
            </div>
            <div style="color: var(--text-dim); font-size: 12px;">${escapeHtml(it.performer || "—")}</div>
          </div>
          <div style="font-weight: 600; margin-left: 12px;">${it.count}</div>
        </div>
      `).join("");
    }
  } catch {}
}

async function renderDashStreams() {
  try {
    const s = await apiGet("/api/streams");
    qs("#dash-streams-count").textContent = s.streams.length;
    const list = qs("#dash-streams-list");
    if (!s.streams.length) {
      list.innerHTML = `<div class="field-help">No active streams.</div>`;
      return;
    }
    list.innerHTML = s.streams.map((st) => {
      const started = st.started ? new Date(st.started * 1000).toLocaleTimeString() : "—";
      return `
        <div class="profile-row" style="flex-direction: column; align-items: stretch; gap: 4px;">
          <div class="profile-name">${escapeHtml(st.title || st.id)}</div>
          <div style="display: flex; justify-content: space-between; color: var(--text-dim); font-size: 12px;">
            <span>${escapeHtml(st.performer || "")}</span>
            <span>Started ${started}</span>
          </div>
        </div>
      `;
    }).join("");
  } catch {}
}

async function renderDashLogs() {
  try {
    const s = await apiGet("/api/logs?limit=20");
    const pane = qs("#dash-recent-logs");
    if (!s.entries.length) {
      pane.textContent = "(no log entries yet)";
      return;
    }
    pane.textContent = s.entries
      .slice(-20)
      .map((e) => `${e.timestamp} [${e.level}] ${e.message}`)
      .join("\n");
    pane.scrollTop = pane.scrollHeight;
  } catch {}
}

window.init_dashboard = async function () {
  await Promise.all([renderDashStatus(), renderDashLibraryAndUsage(), renderDashStreams(), renderDashLogs()]);

  qs("#dash-streams-card").addEventListener("click", () => {
    qs("#dash-streams-list")?.scrollIntoView({ behavior: "smooth" });
  });
  qs("#dash-reset-stats-btn").addEventListener("click", async () => {
    if (!confirm("Reset proxy statistics? This clears play counts and auth counters.")) return;
    try {
      await apiPost("/api/stats/reset", {});
      toast("Statistics reset.", "success");
      await renderDashLibraryAndUsage();
    } catch (e) {
      toast(`Reset failed: ${e.message}`, "error");
    }
  });
  qs("#dash-migration-dismiss").addEventListener("click", () => {
    qs("#dash-migration-banner").classList.add("hide");
    qs("#dash-migration-banner")._dismissed = true;
  });
};

window.show_dashboard = async function () {
  /* Kick an immediate refresh and re-arm the pollers each time the tab
     becomes visible. Previous intervals (from a prior visit) are cleared
     first so we don't accumulate them. */
  if (dashState.statsInterval) clearInterval(dashState.statsInterval);
  if (dashState.streamsInterval) clearInterval(dashState.streamsInterval);
  if (dashState.logsInterval) clearInterval(dashState.logsInterval);

  await Promise.all([renderDashStatus(), renderDashLibraryAndUsage(), renderDashStreams(), renderDashLogs()]);

  dashState.statsInterval   = setInterval(renderDashLibraryAndUsage, 60000);
  dashState.streamsInterval = setInterval(renderDashStreams, 5000);
  dashState.logsInterval    = setInterval(renderDashLogs, 10000);
};

/* ============================================================ */
/* Players tab                                                  */
/* ============================================================ */
const playersState = {
  profiles: [],
  uaRefresh: null,
  editingName: null,    // profile name currently open in the editor (null → create)
};

function relativeTime(ageSec) {
  if (ageSec < 60) return `${ageSec}s ago`;
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`;
  if (ageSec < 86400) return `${Math.floor(ageSec / 3600)}h ago`;
  return `${Math.floor(ageSec / 86400)}d ago`;
}

function profileBadgeClass(name) {
  return /^(swiftfin|infuse|senplayer|default)$/.test(name) ? name : "default";
}

async function renderUaList() {
  const list = qs("#players-ua-list");
  try {
    const data = await apiGet("/api/players/ua-log");
    if (!data.entries.length) {
      list.innerHTML = `<div class="field-help">No clients have connected yet.</div>`;
      return;
    }
    list.innerHTML = data.entries.map((e) => `
      <div class="profile-row" style="flex-direction: column; align-items: stretch; gap: 6px;">
        <div class="profile-name" style="word-break: break-all;">${escapeHtml(e.userAgent)}</div>
        <div style="display: flex; justify-content: space-between; align-items: center; color: var(--text-dim); font-size: 12px;">
          <span>Last seen: ${relativeTime(e.ageSeconds)}</span>
          <span>Profile: <span class="profile-badge ${profileBadgeClass(e.profile)}">${escapeHtml(e.profile)}</span></span>
          <button class="icon-btn" data-copy="${encodeURIComponent(e.userAgent)}" title="Copy User-Agent">⧉</button>
        </div>
      </div>
    `).join("");
    qsa("[data-copy]", list).forEach((btn) => {
      btn.addEventListener("click", () => {
        navigator.clipboard.writeText(decodeURIComponent(btn.dataset.copy));
        toast("User-Agent copied to clipboard", "success");
      });
    });
  } catch (e) {
    list.innerHTML = `<div class="field-help" style="color: var(--err, #e86464);">Failed to load: ${escapeHtml(e.message)}</div>`;
  }
}

async function renderProfileList() {
  const list = qs("#players-profile-list");
  try {
    const data = await apiGet("/api/players/profiles");
    playersState.profiles = data.profiles;
    if (!data.profiles.length) {
      list.innerHTML = `<div class="field-help">No profiles configured.</div>`;
      return;
    }
    list.innerHTML = data.profiles.map((p) => `
      <div class="profile-row">
        <div>
          <div class="profile-name">[${escapeHtml(p.name)}]</div>
          <div style="color: var(--text-dim); font-size: 12px;">
            ${p.isDefault ? "— default —" : `match: ${escapeHtml(p.userAgentMatch || "(empty)")}`}
            · ${escapeHtml(p.performerType)} · ${escapeHtml(p.posterFormat)}
          </div>
        </div>
        <div class="profile-actions">
          <button class="icon-btn" data-edit="${escapeHtml(p.name)}" title="Edit">✎</button>
          ${p.isDefault ? "" : `<button class="icon-btn danger" data-delete="${escapeHtml(p.name)}" title="Delete">🗑</button>`}
        </div>
      </div>
    `).join("");
    qsa("[data-edit]", list).forEach((btn) => btn.addEventListener("click", () => openProfileEditor(btn.dataset.edit)));
    qsa("[data-delete]", list).forEach((btn) => btn.addEventListener("click", () => confirmDeleteProfile(btn.dataset.delete)));
  } catch (e) {
    list.innerHTML = `<div class="field-help" style="color: var(--err, #e86464);">Failed to load: ${escapeHtml(e.message)}</div>`;
  }
}

function openProfileEditor(name) {
  const modal = qs("#profile-editor-modal");
  const existing = playersState.profiles.find((p) => p.name === name);
  playersState.editingName = existing ? existing.name : null;

  qs("#profile-editor-title").textContent = existing
    ? `Edit Player Profile: ${existing.name}`
    : "Add Player Profile";
  const nameInput = qs("#profile-editor-name");
  nameInput.value = existing ? existing.name : "";
  nameInput.disabled = !!(existing && existing.isDefault);
  qs("#profile-editor-ua").value = existing ? existing.userAgentMatch : "";
  qs("#profile-editor-ua").disabled = !!(existing && existing.isDefault);

  const perf = existing ? existing.performerType : "BoxSet";
  qsa('input[name=profile-perf]').forEach((r) => r.checked = r.value === perf);

  const poster = existing ? existing.posterFormat : "landscape";
  qsa('input[name=profile-poster]').forEach((r) => r.checked = r.value === poster);

  modal.classList.add("open");
}

function closeProfileEditor() {
  qs("#profile-editor-modal").classList.remove("open");
  playersState.editingName = null;
}

async function saveProfileEditor() {
  const name = qs("#profile-editor-name").value.trim().toLowerCase();
  const ua = qs("#profile-editor-ua").value.trim();
  const perfRadio = qs('input[name=profile-perf]:checked');
  const posterRadio = qs('input[name=profile-poster]:checked');
  if (!name || !/^[a-z0-9_]+$/.test(name)) {
    toast("Profile name must be lowercase letters/digits/underscore", "error");
    return;
  }
  try {
    await apiPost("/api/players/profile", {
      name,
      userAgentMatch: ua,
      performerType: perfRadio ? perfRadio.value : "BoxSet",
      posterFormat: posterRadio ? posterRadio.value : "landscape",
    });
    toast(`Profile ${name} saved.`, "success");
    closeProfileEditor();
    await renderProfileList();
  } catch (e) {
    toast(`Save failed: ${e.message}`, "error");
  }
}

async function confirmDeleteProfile(name) {
  const existing = playersState.profiles.find((p) => p.name === name);
  const ua = existing ? existing.userAgentMatch : "";
  const msg = `Delete profile [${name}]? Clients matching '${ua || "(empty)"}' will fall back to [default].`;
  if (!confirm(msg)) return;
  try {
    await apiPost("/api/players/profile/delete", { name });
    toast(`Profile ${name} deleted.`, "success");
    await renderProfileList();
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "error");
  }
}

window.init_players = async function () {
  await Promise.all([renderUaList(), renderProfileList()]);
  qs("#players-add-btn").addEventListener("click", () => openProfileEditor(null));
  qs("#profile-editor-close").addEventListener("click", closeProfileEditor);
  qs("#profile-editor-cancel").addEventListener("click", closeProfileEditor);
  qs("#profile-editor-save").addEventListener("click", saveProfileEditor);
  qs("#profile-editor-modal").addEventListener("click", (e) => {
    if (e.target.id === "profile-editor-modal") closeProfileEditor();   // click-outside
  });
};

window.show_players = async function () {
  await Promise.all([renderUaList(), renderProfileList()]);
  if (playersState.uaRefresh) clearInterval(playersState.uaRefresh);
  playersState.uaRefresh = setInterval(renderUaList, 30000);
};

/* ============================================================ */
/* Search tab                                                   */
/* ============================================================ */
window.init_search = async function () {
  try { await loadConfig(); } catch {}
  bindFormFromConfig(pageRoot("search"));
};
window.show_search = async function () {
  try { await loadConfig(); bindFormFromConfig(pageRoot("search")); } catch {}
};

/* ============================================================ */
/* Playback tab                                                 */
/* ============================================================ */
const SORT_OPTIONS = [
  ["DateCreated", "Date Added"],
  ["SortName", "Name"],
  ["CommunityRating", "Rating"],
  ["PlayCount", "Scene Count"],
  ["Random", "Random"],
];

function populateSortDefaults() {
  const root = pageRoot("playback");
  if (!root) return;
  qsa("select[data-sort-options]", root).forEach((sel) => {
    if (sel._populated) return;
    sel._populated = true;
    SORT_OPTIONS.forEach(([val, label]) => {
      const opt = document.createElement("option");
      opt.value = val;
      opt.textContent = label;
      sel.appendChild(opt);
    });
  });
}

function updateHeroVisibility() {
  const root = pageRoot("playback");
  if (!root) return;
  const source = qs('[data-key=HERO_SOURCE]', root).value;
  qsa("[data-shows-for-hero]", root).forEach((el) => {
    el.style.display = (el.dataset.showsForHero === source) ? "" : "none";
  });
}

window.init_playback = async function () {
  try { await loadConfig(); } catch {}
  populateSortDefaults();           // before bind, so options exist
  bindFormFromConfig(pageRoot("playback"));
  updateHeroVisibility();
  qs('[data-key=HERO_SOURCE]').addEventListener("change", updateHeroVisibility);
};

window.show_playback = async function () {
  try { await loadConfig(); populateSortDefaults(); bindFormFromConfig(pageRoot("playback")); updateHeroVisibility(); } catch {}
};

/* ============================================================ */
/* Libraries tab                                                */
/* ============================================================ */
const GENRE_MODE_NOTES = {
  all_tags: "Every tag on a scene becomes a genre. Best for small, curated tag sets.",
  parent_tag: "Only tags that are direct children of your GENRE parent tag become genres. Recommended for large collections.",
  top_n: "The tags with the most scenes become genres automatically. No Stash-side setup required.",
};

function updateGenreModeVisibility() {
  const root = pageRoot("libraries");
  if (!root) return;
  const checked = qs('[data-key=GENRE_MODE] input[type=radio]:checked', root);
  const mode = checked ? checked.value : "";
  qsa("[data-shows-for]", root).forEach((el) => {
    el.style.display = (el.dataset.showsFor === mode) ? "" : "none";
  });
  const note = qs("#genre-mode-note", root);
  if (note) note.textContent = GENRE_MODE_NOTES[mode] || "";
}

window.init_libraries = async function () {
  try { await loadConfig(); } catch {}
  bindFormFromConfig(pageRoot("libraries"));
  updateGenreModeVisibility();

  /* React to radio clicks so the conditional fields show/hide live. */
  qsa('[data-key=GENRE_MODE] input[type=radio]', pageRoot("libraries")).forEach((r) => {
    r.addEventListener("change", updateGenreModeVisibility);
  });

  /* Pattern-test widget — runs the current textarea patterns client-side. */
  qs("#series-pattern-test-btn").addEventListener("click", () => {
    const title = qs("#series-pattern-test-input").value;
    const pats = qs('[data-key=SERIES_EPISODE_PATTERNS]').value
      .split("\n").map((s) => s.trim()).filter(Boolean);
    const result = qs("#series-pattern-test-result");
    if (!title) { result.textContent = "Enter a title to test."; result.style.color = ""; return; }
    for (let i = 0; i < pats.length; i++) {
      try {
        const re = new RegExp(pats[i], "i");
        const m = title.match(re);
        if (m && m.length >= 3) {
          result.textContent = `✓ Matched pattern ${i + 1}: Season ${m[1]}, Episode ${m[2]}`;
          result.style.color = "var(--ok, #36c563)";
          return;
        }
      } catch (e) {
        result.textContent = `✗ Pattern ${i + 1} invalid: ${e.message}`;
        result.style.color = "var(--err, #e86464)";
        return;
      }
    }
    result.textContent = "✗ No match — would go to Season 0";
    result.style.color = "var(--warn, #e8a864)";
  });
};

window.show_libraries = async function () {
  try { await loadConfig(); bindFormFromConfig(pageRoot("libraries")); updateGenreModeVisibility(); } catch {}
};

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
