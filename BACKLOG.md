# Future Additions

Backlog of scoped-but-not-yet-built features. Each entry includes what,
why, rough cost, and any known caveats so we don't relitigate the same
decision every time it comes up in conversation.

Items live here until they're either (a) promoted into a phase / sprint
and executed, or (b) explicitly declined and moved to an "Abandoned"
section below.

---

## Bundle `jellyfin-web` to support the official Jellyfin mobile apps

**Status:** deferred (2026-04-24). Current stance: this server only
works with a dedicated Jellyfin-compatible *media player* — Swiftfin,
Infuse, or SenPlayer. Official Jellyfin iOS / iPadOS / Android apps are
intentionally unsupported because they load the server's web UI in a
WebView rather than using the Jellyfin API directly.

**What:** the official Jellyfin mobile apps are WebView wrappers. After
validating a server via `/System/Info/Public`, they open a WebView
pointed at the server's root URL and expect `/web/index.html` to serve
the Jellyfin web client. We don't ship that bundle, so the WebView
renders whatever HTML we return at `/` — currently the small landing
page from `endpoints/system.py::endpoint_root()`.

**Why it's not a redirect fix:** the apps aren't "looking for identity at
`/`"; they're loading `/` as a web surface. Nothing short of actually
serving a Jellyfin-compatible web bundle at `/web/*` will make the app
behave like it's connected to a real Jellyfin server.

**Implementation sketch:**
1. Multi-stage `build_docker/Dockerfile`: copy `/jellyfin/jellyfin-web/`
   out of the official `jellyfin/jellyfin:latest` image into our image
   at `/app/web/`. (Already working in `Dockerfile.jfweb` for the dev
   stack — the main Dockerfile would mirror it.)
2. Starlette mount `/web/*` as a `StaticFiles` app rooted at `/app/web/`.
3. Override `/web/config.json` with a dynamic handler that synthesizes
   `{servers: [derive_local_address(request)], multiserver: false}`
   so new sessions land on this proxy automatically.
4. Change `endpoint_root` to `RedirectResponse("/web/index.html")`
   — matches real Jellyfin's root behaviour.

**Cost:**
- Image size: ~50 MB increase from the web bundle.
- Dockerfile change is small (the extract pattern already exists).
- No new Python dependencies; Starlette's StaticFiles is already there.

**Caveats:**
- `jellyfin-web` hits API endpoints Infuse / Swiftfin / SenPlayer never
  touch: admin dashboard, live TV, metadata editor, scheduled tasks,
  playback queue manipulation, session/socket orchestration, quick
  connect. Most will 404 or return empty — acceptable for the browse +
  play surface, user-visible for the admin surfaces. Each one is a
  potential follow-up stub.
- Pin the `jellyfin/jellyfin` version used in the extract stage so
  upstream changes don't silently break our bundle.

---

## Abandoned

*(none yet)*
