# Stash-Jellyfin Proxy

**Version 7.3.5**

A Python proxy server that lets Jellyfin-compatible media players browse and stream a [Stash](https://stashapp.cc/) library by emulating the Jellyfin HTTP API.

## Supported Clients

The proxy is designed for **dedicated Jellyfin-compatible media players**. The official Jellyfin iOS / iPadOS / Android apps are intentionally unsupported — they load the server's web UI in a WebView and require a `jellyfin-web` bundle the proxy doesn't ship.

| Client     | Platform           | Status            |
|------------|--------------------|-------------------|
| Infuse     | iOS / tvOS / macOS | Fully supported   |
| Swiftfin   | iOS / tvOS         | Fully supported   |
| SenPlayer  | iOS                | Fully supported   |
| Other Jellyfin-compatible third-party players | Various | May work; untested |

Per-client behavior (poster aspect, performer item type, library `CollectionType` for Series) is selected automatically by User-Agent and is fully configurable via the **Players** tab in the Web UI.

## Features

### Library
- **Full Stash integration**: Scenes, Performers, Studios, Groups, Tags
- **Series detection**: studios tagged with `SERIES_TAG` (default `Series`) become a `Shows` library — Swiftfin renders native Series → Season → Episode navigation; other clients see a regular collection of "shows" (configurable per-profile)
- **Playlists**: full create / rename / add / remove / delete from clients that expose playlist UI (Infuse, Jellyfin web). Backed by a Stash parent tag (`PLAYLIST_PARENT_TAG`, default `Playlists`) — each child tag is one playlist, its tagged scenes are its items. Swiftfin and SenPlayer get a read-only `BoxSet`-shaped view (their UI doesn't render the native Playlist type)
- **Tag-based libraries** (`TAG_GROUPS`): any Stash tag can become a top-level browsable folder
- **Saved Filters**: browse your Stash saved filters as folders, with sort parameters translated to GraphQL
- **Configurable Genres**: three modes for what shows up under "Genres" — every tag (`all_tags`), only descendants of a parent tag (`parent_tag`, default), or the top-N by scene count (`top_n`)
- **Filter panel** (Swiftfin): Years, Genres, Tags, Liked, Played — with hierarchy-aware tag filtering (depth: -1) and AND/OR genre logic
- **Per-library default sort**: separate defaults for Scenes / Studios / Performers / Groups / Tag Groups / Saved Filters when the client doesn't specify one

### Playback
- **Direct streaming** via async `httpx` with byte-range forwarding — no buffering layer
- **Subtitles**: SRT and VTT delivered from Stash captions
- **Rich metadata**: codec details, resolution, bitrate, frame rate, channel layout, container, video type
- **Play / resume / watched sync**: read from and written back to Stash. Scenes >90% watched are auto-marked played; otherwise resume position is saved

### Imagery
- **Aspect-aware image endpoint**: real portrait crops with configurable anchor (`POSTER_CROP_ANCHOR`); landscape sources are padded or cropped to the requested aspect rather than squashed
- **Per-client poster format**: each player profile picks portrait vs landscape posters and the performer item type (`Person` vs custom)
- **Library tiles**: scene-screenshot tiles with a 50% dim + label overlay; the same composite is applied to TAG_GROUPS folders
- **Studio logo fallback**: scenes inside SERIES studios prefer the parent studio's logo over the scene screenshot
- **Cache-busting `ImageTag`**: per-process tag rotation forces native clients (which key images by `(ItemId, ImageTag)`) to refresh on restart

### Home / Hero / Banner
- **Configurable hero source**: `recent` / `random` / `favorites` / `top_rated` / `recently_watched`
- **SenPlayer banner**: random scenes (with screenshots) drive SenPlayer's rotating home banner — choose a `recent` or `tag`-based pool

### Favorites
- **Scenes** and **Groups**: tag-based via `FAVORITE_TAG` (auto-created in Stash on first toggle, case-insensitive match against existing tags). `movieUpdate` mutation under the hood for groups.
- **Performers**: native Stash `favorite` boolean
- **Studios**: `studioUpdate` mutation
- All favorite toggles return a full `UserItemDataDto` so client UI reconciles correctly without a navigation round-trip.

### Web UI (port 8097)
8-tab configuration dashboard — every config key is reachable in the UI, no more hand-editing the conf file:

- **Dashboard** — proxy + Stash status, active streams, lifetime stats, recent log tail
- **Connection** — Stash URL / API key / GraphQL path / TLS, client credentials, with a live Test Connection probe
- **Libraries** — TAG_GROUPS, LATEST_GROUPS, Genres mode, Series detection (with regex tester for episode parsing)
- **Players** — live User-Agent feed of recent clients + a profile editor for per-client image policy
- **Playback** — hero source, default sort per library, banner mode
- **Search** — scope toggles (scenes / performers / studios / groups), filter panel limits and logic
- **System** — server identity, performance (timeouts, page sizes, image cache size), logging, security (auth + IP banning), restart control
- **Logs** — filterable viewer with download and Copy button

### Operations
- **Hot config reload** via SIGHUP — Web UI saves rewrite the conf file in place and reload without dropping connections
- **v1 → v2 config migration** runs once on startup; old configs are auto-upgraded with a UI banner summarizing what changed
- **IP banning** for failed auth attempts (configurable threshold + rolling window)
- **Stream tracking** — every active stream visible in the Dashboard
- **Persisted stats** — proxy_stats.json tracks lifetime counts across restarts
- **Docker** — single image with PUID/PGID + TZ; published to GHCR on every `main` push

## Quick Start

### Standalone

Requires **Python 3.10+**.

```bash
pip install hypercorn starlette httpx Pillow setproctitle
python -m stash_jellyfin_proxy
```

Or, after `pip install -e .`:

```bash
stash-jellyfin-proxy
```

Then:

1. Open the Web UI at `http://localhost:8097`
2. Fill in `STASH_URL`, `STASH_API_KEY`, `SJS_USER`, `SJS_PASSWORD` on the Connection tab
3. Add the server in your Jellyfin client at `http://your-server:8096`

### Docker

```bash
docker run -d \
  --name stash-jellyfin-proxy \
  -p 8096:8096 \
  -p 8097:8097 \
  -v /path/to/config:/config \
  -e PUID=1000 \
  -e PGID=1000 \
  -e TZ=America/New_York \
  ghcr.io/feldorn/stash-jellyfin-proxy:latest
```

Image entrypoint runs `python -m stash_jellyfin_proxy` against `/config/stash_jellyfin_proxy.conf`.

## Configuration

`stash_jellyfin_proxy.conf` location: working directory by default, or set via `CONFIG_FILE` env var or `--config /path/to.conf`. The Web UI rewrites this file in place.

The full list lives in the conf file and the Web UI; the most common keys:

### Connection
| Key | Default | Description |
|---|---|---|
| `STASH_URL` | `http://localhost:9999` | Stash server URL |
| `STASH_API_KEY` | *(required)* | from Stash → Settings → Security |
| `STASH_GRAPHQL_PATH` | `/graphql` | use `/graphql-local` if Stash sits behind a SWAG reverse proxy |
| `STASH_VERIFY_TLS` | `false` | set `true` if Stash has a real cert |
| `SJS_USER` / `SJS_PASSWORD` | *(required)* | client login |
| `PROXY_PORT` | `8096` | Jellyfin API port |
| `UI_PORT` | `8097` | Web UI port (`0` to disable) |

### Library
| Key | Default | Description |
|---|---|---|
| `TAG_GROUPS` | empty | comma-separated tags shown as top-level folders |
| `LATEST_GROUPS` | `Scenes` | which folders feed Infuse "Recently Added" |
| `FAVORITE_TAG` | empty | tag used for scene + group favorites (e.g. `Favorite`) |
| `SERIES_TAG` | `Series` | studios tagged with this become Series libraries |
| `SERIES_EPISODE_PATTERNS` | empty | newline-separated regex chain for parsing `S##E##` from titles |
| `PLAYLIST_PARENT_TAG` | `Playlists` | parent tag whose direct children become Jellyfin playlists. Empty disables the feature |
| `ENABLE_FILTERS` | `true` | show Saved Filters folder |
| `ENABLE_TAG_FILTERS` | `false` | show Tags root folder |
| `ENABLE_ALL_TAGS` | `false` | include "All Tags" subfolder (slow with many tags) |

### Genres / Filter panel
| Key | Default | Description |
|---|---|---|
| `GENRE_MODE` | `parent_tag` | `all_tags` / `parent_tag` / `top_n` |
| `GENRE_PARENT_TAG` | `GENRE` | parent tag whose descendants become Genres |
| `GENRE_TOP_N` | `25` | for `top_n` mode |
| `FILTER_TAGS_MAX` | `50` | max entries per dimension in `/Items/Filters` |
| `GENRE_FILTER_LOGIC` | `AND` | `AND` (INCLUDES_ALL) or `OR` (INCLUDES) |
| `FILTER_TAGS_WALK_HIERARCHY` | `true` | a selected tag also matches its descendants |

### Search scope
| Key | Default |
|---|---|
| `SEARCH_INCLUDE_SCENES` / `_PERFORMERS` / `_STUDIOS` / `_GROUPS` | all `true` |

### Hero / banner
| Key | Default | Description |
|---|---|---|
| `HERO_SOURCE` | `recent` | `recent` / `random` / `favorites` / `top_rated` / `recently_watched` |
| `HERO_MIN_RATING` | `75` | minimum `rating100` for `top_rated` mode |
| `BANNER_MODE` | `recent` | SenPlayer banner pool: `recent` or `tag` |
| `BANNER_POOL_SIZE` | `200` | size of the random pool in `recent` mode |
| `BANNER_TAGS` | empty | comma-separated tags for `tag` mode |

### Per-library default sort
| Key | Default |
|---|---|
| `SCENES_DEFAULT_SORT` | `DateCreated` |
| `STUDIOS_DEFAULT_SORT` | `SortName` |
| `PERFORMERS_DEFAULT_SORT` | `SortName` |
| `GROUPS_DEFAULT_SORT` | `SortName` |
| `TAG_GROUPS_DEFAULT_SORT` | `PlayCount` |
| `SAVED_FILTERS_DEFAULT_SORT` | `PlayCount` |

### Image / metadata policy
| Key | Default | Description |
|---|---|---|
| `POSTER_CROP_ANCHOR` | `center` | crop anchor for portrait conversion |
| `OFFICIAL_RATING` | `NC-17` | string reported as `OfficialRating` |
| `SORT_STRIP_ARTICLES` | `The, A, An` | leading articles stripped for `SortName` |
| `ENABLE_IMAGE_RESIZE` | `true` | requires Pillow (always installed) |
| `IMAGE_CACHE_MAX_SIZE` | `100` | Pillow output cache entries |

### Player profiles
Per-client behavior is configured in INI-style `[player.<name>]` sections of the conf file (or via the Players tab in the Web UI). Each profile matches against User-Agent (substring, first-win, with a default fallback) and sets:

```
[player.swiftfin]
ua_match = Swiftfin
performer_item_type = Person
scene_poster_format = portrait
series_collection_type = tvshows
```

Unique UAs are logged to `<LOG_DIR>/ua_log.json` and surfaced in the Web UI for one-click profile creation.

### Performance / Logging / Security
| Key | Default |
|---|---|
| `STASH_TIMEOUT` / `STASH_RETRIES` | `30` / `3` |
| `DEFAULT_PAGE_SIZE` / `MAX_PAGE_SIZE` | `50` / `200` |
| `LOG_DIR` / `LOG_FILE` / `LOG_LEVEL` | `.` / `stash_jellyfin_proxy.log` / `INFO` |
| `LOG_MAX_SIZE_MB` / `LOG_BACKUP_COUNT` | `10` / `3` |
| `REQUIRE_AUTH_FOR_CONFIG` | `false` |
| `BAN_THRESHOLD` / `BAN_WINDOW_MINUTES` | `10` / `15` |
| `JELLYFIN_VERSION` | `10.11.0` |

Settings can also be set via environment variables (same names) — env vars win over the conf file and are shown read-only in the Web UI.

## Connecting Clients

In each client, add a Jellyfin server pointed at `http://your-server:8096` and log in with `SJS_USER` / `SJS_PASSWORD`.

- **Infuse** — add a share, choose Jellyfin as the type
- **Swiftfin** — add server, log in. Series studios appear under a `tvshows` library with native Series / Season / Episode navigation.
- **SenPlayer** — add a Jellyfin/Emby server. The home banner cycles through randomized scene screenshots (configurable).

## Architecture

```
Jellyfin client (Infuse / Swiftfin / SenPlayer)
        │
        ▼
   stash-jellyfin-proxy ── port 8096 (Jellyfin API)
   ─ Starlette + Hypercorn
   ─ async httpx → Stash GraphQL
   ─ per-client Player Profiles
   ─ TTLCache for connection state + filter cache
        │
        ▼
   Stash GraphQL API (port 9999)
```

The package is organized topically:

```
stash_jellyfin_proxy/
  __main__.py                  entry point + startup sequence
  runtime.py                   shared mutable state (single source of truth)
  app.py                       Starlette app + middleware stack
  errors.py                    StashUnavailable / StashError + handlers
  cache/ttl.py                 TTLCache
  config/                      bootstrap, loader, helpers, v1→v2 migration
  endpoints/                   items, images, playback, stream, search, user_actions, views, stubs
  mapping/                     scene → Jellyfin item shape, image policy, user DTO
  middleware/                  auth, request logging (pure ASGI), case-insensitive paths
  players/                     Profile dataclass + UA matcher with capture
  state/                       persisted stats, live stream tracking
  stash/                       async client + GraphQL helpers
  ui/                          Web UI handlers + templates
  util/                        ID helpers, image (PIL) helpers, episode-title parsing
```

Streaming uses `httpx.AsyncClient.send(stream=True)` + `aiter_bytes()` — byte ranges are forwarded directly, no buffering. The request-logging middleware is pure ASGI (not `BaseHTTPMiddleware`) so it doesn't wrap the response body.

## Requirements

- Python 3.10+
- Stash media server with API access enabled
- Dependencies: `hypercorn`, `starlette`, `httpx`, `Pillow`, `setproctitle` — installed automatically via `pip install -e .`

## Known Limitations

- **Single-user authentication**: one set of `SJS_USER` / `SJS_PASSWORD` credentials shared by every client.
- **Image cache busting on native clients**: clients key images by `(ItemId, ImageTag)` and ignore HTTP cache headers. The proxy rotates `ImageTag` per process restart so artwork refreshes; clearing the client's metadata cache is still the surest fix if a specific image gets stuck.
- **Official Jellyfin apps unsupported**: those apps require the `jellyfin-web` WebView bundle, which the proxy doesn't ship. See `BACKLOG.md` for the deferred design.
- **Series CollectionType is per-client**: only Swiftfin gets native `tvshows` navigation. Infuse and SenPlayer fall back to a flat BoxSet because their `tvshows` renderer shows a blank folder.

## Changelog

### v7.3.5

Closes [#26](https://github.com/feldorn/Stash-Jellyfin-Proxy/issues/26) (@stashcollection14) — Stash's per-scene "total play duration" column stayed at zero because the proxy was never sending the `playDuration` argument on `sceneSaveActivity`. Play count and progress updated correctly (fixed in v7.3.4); duration did not.

**Root cause.** Stash's `sceneSaveActivity(id, resume_time, playDuration)` mutation *accumulates* the `playDuration` argument into the scene's total. Our three call sites in `endpoints/views.py` (Progress, Stopped >90%, Stopped ≤90%) only passed `resume_time`, so play_duration was never touched.

**Fix.** All three call sites now send `playDuration = wall-clock seconds elapsed since the last event on this stream`. The delta comes from a small helper (`_consume_watched_delta`) that reads `last_progress_time` off the tracked `_active_streams` record, updates it, and returns the delta capped at 60 seconds per event. Using wall-clock time (not position delta) means the count is unaffected by seeking, and is naturally correct through pauses on well-behaved clients (they stop firing Progress events while paused, so no time accrues). The 60-second per-event cap defends against long client-side stalls or delayed Progress events that would otherwise inflate the count.

Log lines on each event now include the delta added so it's easy to spot-check in the log:
```
⏸ Saved resume + recorded play: scene-3814 at 1570s (21%, +14s duration)
▶ Auto-marked played: scene-3814 (100% watched, +8s duration)
```

### v7.3.4

Closes [#25](https://github.com/feldorn/Stash-Jellyfin-Proxy/issues/25) part 2 — partial-play sessions weren't being recorded in Stash's `play_history`. Reporter @tanlidoushen watched to 21% of a scene from Hills Lite, exited, and expected the scene to appear at the top of `?sortby=last_played_at&sortdir=desc`. The resume position was saved correctly, but `play_count` never incremented and no play_history entry was written — so Stash didn't know a play had happened.

**Root cause.** `endpoints/views.py` on `Sessions/Playing/Stopped` only called `sceneAddPlay` when the user watched >90%. Anything less was treated as pure "in progress" — resume position saved, no play recorded. Under this policy, a user who watched half a scene and exited had no evidence of the session in Stash's history.

**Fix.** Any session that stops past a 30-second position threshold now records a play via `sceneAddPlay` (which increments `play_count`, adds a `play_history` entry, and bumps `last_played_at`). Sessions under 30s still just save resume position — 30s is the "brief tap" threshold below which we assume an accidental click or stray seek, not a real watch. Existing >90% auto-mark behavior is unchanged (still records the play and clears the resume position).

**Behavior change to be aware of.** Every existing Infuse / Swiftfin / SenPlayer / Roku user will start seeing partial-watch sessions appear in Stash's play history and reflected in `last_played_at`. That matches how Stash's own web UI, Plex, Trakt, and most media systems track "you watched this" — but if you'd been relying on "only completed watches count," this is the release that changes it.

### v7.3.3

Two of the three issues from [#25](https://github.com/feldorn/Stash-Jellyfin-Proxy/issues/25) (reported by @tanlidoushen). The third — Hills Lite "Continue Watching" — is still under investigation pending a log excerpt from the reporter.

**GraphQL alias notices no longer log as warnings** (#25 issue 1a)
- Stash's `errors` array in GraphQL responses mixes real errors with informational notices — e.g., `"name 'SERIES' is used as alias for '系列'"` when a config name resolves via a Stash alias rather than a primary name. The proxy was logging the entire array at `WARNING`, so users with non-English primary tag names saw spurious noise on every lookup. Notices matching `is used as alias for` now log at `DEBUG`; real errors still log at `WARNING`.

**CJK glyphs on generated library covers** (#25 issue 1b)
- The tag-group virtual-library cover generator (`util/images.py`) uses PIL with DejaVu Sans Bold, which lacks CJK glyphs — Chinese/Japanese/Korean tag names rendered as tofu boxes (`[ ] [ ]`) on the cover art. Added a CJK-capable font path list preferred whenever the label contains any character in the CJK / halfwidth-fullwidth range (codepoint ≥ 0x2E80). Latin-only labels continue to use DejaVu, so existing covers are visually unchanged.
- **Dockerfile:** added `fonts-noto-cjk` so the Noto Sans CJK Bold TTC is available inside the container. Native (non-Docker) installs need to install a CJK font themselves; the picker will find Noto CJK, PingFang, or Hiragino Sans GB automatically at the standard system paths.

### v7.3.2

Fixes issue [#24](https://github.com/feldorn/Stash-Jellyfin-Proxy/issues/24) — `UnicodeDecodeError` on Windows native runs (Docker users were never affected). Reported by @stashcollection14 with the exact one-line fix.

**Root cause**
- Python's `Path.read_text()` and `open(path, 'r')` default to the platform's preferred encoding when none is supplied. On Linux/macOS that's UTF-8; on Windows it's `cp1252`, which can't decode the eyeball emojis (👁 / 🙈) the v7.2.0 dashboard template introduced for the Connect-a-Player password reveal. The proxy crashed at module import on Windows with `'charmap' codec can't decode byte 0x81`.

**Fix**
- Added `encoding='utf-8'` to every text-mode file open/read/write in the production code: the dashboard template (the reported site), config file reads and writes (loader, writer, helpers, migration, the v7.3.1 heal-append, dashboard config saver, banned-IPs writer), the log-tail reader, the stats JSON, and the auth debug dump. The same latent bug lived in 16 places; the reporter just happened to hit the one that crashes at import. Anything that reads or writes user-content text now decodes/encodes UTF-8 explicitly regardless of platform.
- Regression test: `tests/unit/test_encoding.py` locks that `index.html` contains bytes that `cp1252` can't decode, so removing the explicit `encoding='utf-8'` from `ui/api.py` would re-introduce the crash on Windows and fail CI.

### v7.3.1

Closes a gap from v7.3.0: existing v2 installs upgrading to v7.3.0 wouldn't see the new `[player.roku]` profile in their config or the Players tab, because `V2_DEFAULT_PLAYERS` is only consulted during the one-time v1→v2 migration that those installs already ran. Any later release adding a default profile would be invisible to them.

- **Startup heal for missing default player profiles.** After the schema-migration short-circuit (config already at `CONFIG_VERSION = 2`), check `V2_DEFAULT_PLAYERS` for any sections missing from the user's config and append them. Idempotent, additive-only — never modifies or removes existing sections, so a hand-customized `[player.roku]` survives untouched. No-op when the config is read-only (logs the issue and continues). Treats `V2_DEFAULT_PLAYERS` as a living "every install should have" list rather than a frozen v1→v2 snapshot.

### v7.3.0

Roku Jellyfin app support plus a path-matching bug that affected any client sending fully-lowercase URLs. All four commits authored by [@arsfeld](https://github.com/arsfeld) and pulled in from his fork.

**Roku support**
- New `[player.roku]` profile (landscape posters, BoxSet performer type) — only added on fresh installs / v1→v2 migration; existing v2 installs need to add it via the Players tab.
- Four new endpoint stubs the Roku app probes that the iOS-only clients don't: `/System/Configuration/Encoding` (advertises direct-play-only, no transcoding), `/Items/{id}/Images/Logo[/{index}]` (404 quietly — Stash has no logo concept), `/Items/{id}/Images` (advertises Primary + Backdrop; an empty list crashes the Roku app on the detail screen), and an explicit `/Items/Suggestions` route (previously matched `/Items/{item_id}` with `item_id="Suggestions"` and got shipped to Stash GraphQL as a numeric id).

**Path normalization (affects all clients)**
- `CaseInsensitivePathMiddleware` previously only ran template matching when the request path differed from its lowercase form, so any client sending fully-lowercase paths (`/items/scene-11/images`) silently bypassed both the static map and template matcher and fell through to `catch_all`. Roku does this; other clients may too. Now always runs template matching on static-map miss.
- Trailing-slash fallback: `/items/?…` now matches the registered `/Items` route. Routes explicitly registered with a trailing slash (`/Playlists/`) still resolve via the first lookup, so the fallback can't shadow them.

**Playback diagnostics**
- `PlaybackInfo` entry, `PlaybackInfo` response (with container/codec/resolution/bitrate/duration/sub-count), and stream-endpoint entry promoted from DEBUG to INFO. Production INFO logs now show a three-line trace of every playback attempt instead of a silent gap between client navigation and the existing `▶ Stream started` marker. Useful for diagnosing Roku/Streamyfin direct-play-vs-transcode failures.

### v7.2.0

New "Connect a Player" surface on the dashboard and a configurable public address, so the credentials and server URL a player needs are visible in one place instead of scattered across the config.

**Connect a Player**
- Dashboard header button opens a "Connect a Player" modal showing the server address, username, and password, each with a copy button. The password is masked with an eyeball reveal toggle and re-masks when the modal closes. Surfaced as an occasional-use popup rather than an always-on dashboard card.

**Public URL**
- New `PUBLIC_URL` config key (Connection → Public Address, live — no restart) for the externally-reachable Jellyfin API address. The proxy can't auto-detect this — its own IP is an internal Docker address and, behind a reverse proxy like SWAG, the public host/scheme/port live in the proxy — so the Connect card shows the server address only once `PUBLIC_URL` is set, with a prompt otherwise. No misleading auto-guessed address.

**Secret reveal**
- API Key and Client Password fields on the Connection tab gain an eyeball reveal. Since the form leaves secret inputs blank so "blank = unchanged" holds on save, revealing lazily fetches the real value from a new allowlisted `GET /api/config/reveal` endpoint and hiding clears it again — typing a new value keeps the edit.

**Fixes**
- Download Config now works behind a reverse proxy: replaced the top-level `window.location` navigation (which SWAG can intercept and which dropped the same-origin context) with a credentialed `fetch` → Blob download that parses the filename from `Content-Disposition`.

### v7.1.7

Issue #16 (SERVER_ID rotation) + issue #17 (favorites in Infuse) — bundled because the root cause of #17 turned out to be the same family of config-writer bugs as #16.

**Favorites**
- Case-insensitive comparison for `FAVORITE_TAG`. Configuring `FAVORITE_TAG=FAVORITE` against an existing Stash tag named `Favorite` had silently broken `IsFavorite` reads — the proxy applied the tag correctly (Stash's tag lookup is case-insensitive) but on read returned False, so Infuse never reflected the favorite back and never sent a remove.
- Toggle handlers no longer claim success when the Stash write fails (e.g. tag couldn't be created); the response now reflects the actual prior state.

**Config writer**
- Dashboard saves of brand-new keys now insert at global scope (just before the first `[section]` header, above any `# ==== ... ====` divider). Previously the new-key branch appended at the end of the file, where the loader binds `KEY = VALUE` into the trailing section's dict — so a freshly-set `FAVORITE_TAG` ended up as `cfg_sections["player.default"]["FAVORITE_TAG"]` and was invisible at runtime. Insertion logic shared between `save_config_value` (one-key writes) and the dashboard handler (bulk writes) via a `find_global_insert_idx` helper.
- Heal-on-read pre-pass: when the dashboard handler reads the config, it strips any line for a known global key sitting inside a `[section]` block and logs `Hoisting misplaced global key out of [...]: <KEY>`. The next save re-inserts at global scope, so existing files self-heal.
- Comment dedup so per-boot rewrites of `CONFIG_LAST_BOOT_AT` don't accumulate copies of the same comment line.
- Blank-line drift (one extra blank per restart) collapsed before write.

**Config persistence diagnostics**
- `SERVER_ID` and `ACCESS_TOKEN` are persisted on first generation (was being regenerated every boot in v7.0.0, breaking client reconnects — issue #16).
- Cross-restart persistence detector: bootstrap writes `CONFIG_LAST_BOOT_AT` every boot and a one-time `CONFIG_PERSISTENCE_INTRODUCED` marker. Combined with whether `SERVER_ID` was loaded, classify the file as `persisted` / `not_persistent` / `not_writable` / `unverified` and surface on the dashboard. Catches anonymous-volume / tmpfs / missing `/config` mount scenarios that look like save bugs from the user's side. The previous `os.access + open(r+)` writability probe is replaced by a save-and-read-back round trip.
- New dashboard banners for `not_writable` (existing) and `not_persistent` (new), each pointing at the likely cause.

**Version reporting**
- Single `__version__` constant in `stash_jellyfin_proxy/__init__.py`. Dashboard `/api/status`, startup log banner, and HTML brand badge all read it; previously three independent hardcoded strings had drifted across releases (dashboard stuck at v7.0.0, startup banner stuck at v7.1.1).

### v7.1.0

**Playlists**
- New `Playlists` library backed by a configurable parent tag (`PLAYLIST_PARENT_TAG`, default `Playlists`). Each direct child of that tag is one playlist; the scenes carrying that child tag are its items.
- Full Jellyfin `PlaylistsController` surface: create, rename, add/remove items, delete, list users — every mutation guarded so only tags that are direct children of the configured parent can be touched.
- Per-client rendering: native `Playlist` type for Infuse and the Jellyfin web client (full create/edit/delete UI); `BoxSet` shape for Swiftfin and SenPlayer (their UI lacks a native Playlist renderer — they can browse and play but not manage). Profile flag `playlist_native` overrides per-client if needed.
- Playlist tiles render as scene-screenshot composites with the playlist name as label overlay (same look as TAG_GROUPS).
- The playlist parent tag and its children are auto-hidden from the generic Tags listing, search hints, and per-scene Tags / Genres so the marker tags don't bleed into the rest of the UI.

### v7.0.0

The largest release in the project's history — a multi-month refactor (Phases 0 → 5B) plus a wave of post-tag polish.

**Architecture & packaging**
- **Now a proper Python package**. Run with `python -m stash_jellyfin_proxy` or the `stash-jellyfin-proxy` console script. The top-level `stash_jellyfin_proxy.py` launcher is gone — Dockerfile, compose, and CI all invoke the package. **Breaking change for users who pin the Docker `CMD` themselves**; published image is unaffected.
- **Async httpx** throughout — `requests` is no longer a dependency. Streaming is true byte-range pass-through with `aiter_bytes()`.
- Module layout broken out into `endpoints/`, `mapping/`, `middleware/`, `players/`, `stash/`, `state/`, `ui/`, `util/`, `config/`, `cache/`. Single source of truth in `runtime.py`.
- Pure-ASGI request-logging middleware so streams aren't wrapped.
- v1 → v2 config migration runs once at startup with a Web UI banner summarizing changes.
- TTLCache live-tracks Stash connectivity instead of polling per request.
- Global error contract (`StashUnavailable` / `StashError` / `BadRequest`) with consistent JSON shape.
- Characterization test harness + 92 unit tests.

**Series support (Phase 2)**
- Studios tagged `SERIES_TAG` are treated as TV series. Their scenes become Episodes everywhere — list, detail, image, search.
- Per-client `series_collection_type`: Swiftfin → `tvshows` (native Series → Season → Episode nav via `/Shows/{id}/Seasons` + `/Shows/{id}/Episodes`); other clients → `movies` (flat BoxSet).
- Episode-title parsing chain via `SERIES_EPISODE_PATTERNS` with a regex tester in the Web UI.
- Studio/Series detail pages get full About metadata; Season tiles render landscape; Episode posters force landscape.
- Auto-create tags is case-insensitive (config `Series` matches existing `series`).

**Player profiles (Phase 2)**
- Per-client behavior driven by `[player.*]` config sections — UA substring match, first-win, default fallback.
- Profile controls `performer_item_type`, `scene_poster_format`, `series_collection_type`.
- Unique UAs captured to `ua_log.json` and surfaced in the Web UI for one-click profile creation.

**Imagery (Phase 3)**
- Aspect-aware image endpoint with real portrait cropping and configurable anchor (`POSTER_CROP_ANCHOR`).
- Studio logo preferred over scene screenshot in the SERIES fallback chain.
- Library tiles redesigned: scene-screenshot background + 50% dim + label overlay, applied to library roots and TAG_GROUPS.
- `ImageTag` rotation per process restart busts native client image caches.

**Genres & filter panel (Phase 3 §7.1, Phase 4 §8.5)**
- `GENRE_MODE`: `all_tags` / `parent_tag` / `top_n` with `GENRE_PARENT_TAG` / `GENRE_TOP_N`.
- Swiftfin filter drawer: Years, Genres, Tags, Liked, Played — honored throughout `/Items` paths and search.
- AND/OR genre logic; hierarchy-aware tag filter (depth: -1).
- Genres + Tags sorted alphabetically in display and per-scene.

**Home / hero (Phase 4 §8.2, §8.4)**
- `HERO_SOURCE` configurable across `recent` / `random` / `favorites` / `top_rated` / `recently_watched` with `HERO_MIN_RATING`.
- Per-library default sort (`SCENES_DEFAULT_SORT`, `STUDIOS_DEFAULT_SORT`, etc.) for clients that don't specify SortBy.
- Phase 4 §8 Home tab + filter panel + sort defaults + library art finalized.

**Metadata (Phase 3 §7.2)**
- Sort article stripping (`SORT_STRIP_ARTICLES`) so "The X" sorts under X.
- `OFFICIAL_RATING` exposed as a config key (default `NC-17`).
- Scene metadata: full About panel content, taglines, parent-studio data on detail pages.

**Web UI (Phase 5A + 5B)**
- 8-tab sidebar nav replacing the single-page UI: Dashboard, Connection, Libraries, Players, Playback, Search, System, Logs.
- Live Test Connection probe, per-client Player Profile editor with live UA feed, client-side Series-Episode regex tester.
- Real-time dashboard with active streams + top-played scenes + persisted lifetime stats.
- HTML/CSS/JS extracted to template + `/static/app.css` + `/static/app.js`.
- Save-behavior badges with consistent symbology and hover tooltips.
- Logs tab with filter, download, and Copy button.

**Post-tag polish**
- Library-root `ImageTag` rotation extended to TAG_GROUPS tiles.
- Swiftfin: filter params honored in search + global `/Items` paths; rail-probe leak fixed on studio + group pages; CollectionType + LATEST_GROUPS startup crash fixed; `/Shows` endpoints wired; performer page no longer shows 7 empty category rails.
- SenPlayer: favorites toggle response now full `UserItemDataDto`; banner shows randomized scenes via `BANNER_MODE` / `BANNER_POOL_SIZE` / `BANNER_TAGS`.
- Findroid / iPad clients: missing endpoints stubbed; ImageBlurHashes set on all BoxSet folder items.
- Stop redirecting `/` to `/System/Info/Public` — the official Jellyfin app's startup probe relied on `/`.
- Series root tile no longer renders blank (missing `MENU_ICONS` entry).
- `/Items//` double-slash warning hardening.

### v6.02
- **SenPlayer home banner**: SenPlayer's rotating banner is now driven by randomized scenes with screenshots — two modes, `recent` and `tag`, exposed via the Web UI.
- **Unique per-scene `ImageTag` and `Etag`**: distinct `ImageTags.Primary` (`p<id>`) and `BackdropImageTags` (`b<id>`) per scene; `Etag` derived from play state so clients refetch when state changes.
- **Favorite toggle fix**: `POST` / `DELETE` on `/Users/{userId}/FavoriteItems/{id}` (and the `UserFavoriteItems` aliases) returns a full `UserItemDataDto` so client UI reconciles without a navigation round-trip.
- **DateLastContentAdded sort**: SenPlayer's default sort key for Studios / Performers / Groups now maps to `created_at`.

### v6.01
- **Group favorites** via the same `FAVORITE_TAG` tag-toggle approach used for scenes (`movieUpdate` mutation + `tags { name }` in every group query).

### v6.00
- Multi-client support (Infuse, SenPlayer fully; Swiftfin and others partial — closed out completely in v7.0.0).
- Full Swiftfin compatibility pass: `/UserFavoriteItems/` aliases, `ImageBlurHashes` on every BoxSet item.
- Play / resume / watched sync — `play_count`, `resume_time`, `last_played_at` round-trip with Stash. >90% watched = auto-played + cleared resume.
- Tag-based favorites for scenes (replaces the broken `organized` approach), native field for performers, `studioUpdate` for studios.
- `RunTimeTicks` always present in `MediaSources`; stop handler resolves duration from Stash if the client posts 0.
- Android client support: case-insensitive path middleware; `/ClientLog/Document` stub.
- Rich `MediaStreams` metadata (codec, resolution, bitrate, frame rate, channel layout).

### v5.04
- Sort support across Performers / Studios / Groups / Tags / saved filters.
- Removed genre/tag cap.

### v5.03
- Partial-date ISO-8601 fix for scenes that previously failed to load.
- Performer `PrimaryImageTag` set to null for performers without images.

### v5.02
- Rich `MediaStreams` metadata.
- Subtitle delivery (SRT / VTT).
- Saved Filters browsing.
- Performer / Studio / Group image serving.
- Tag-based library folders.

### v5.00
- Initial release: Jellyfin API emulation, Stash GraphQL integration, Web UI, Docker.

## License

MIT — see `LICENSE`.
