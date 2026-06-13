"""Stash-Jellyfin Proxy — Jellyfin-API emulation in front of a Stash
media server. Entry point is `python -m stash_jellyfin_proxy`, which
invokes `__main__.main()`.

Subpackage layout (see CLAUDE.md §Architecture for details):

    config/     — config file loading, migration, bootstrap
    endpoints/  — Jellyfin API handlers (items, stream, images, ...)
    mapping/    — Stash → Jellyfin data shape conversion
    middleware/ — auth, request logging, path canonicalization
    players/    — per-client Profile dataclass + UA matcher
    stash/      — GraphQL client + query helpers
    state/      — runtime-only in-process state (streams, stats)
    ui/         — Web UI HTML + /api/* handlers
    util/       — small helpers (ids, images, series parser)

runtime.py holds all shared config + mutable state (single source of
truth; see its docstring).
"""

# Single source of truth for the package version. Keep in sync with
# pyproject.toml `[project].version`. The startup banner, dashboard
# API, and HTML brand badge all read this constant.
__version__ = "7.3.2"
