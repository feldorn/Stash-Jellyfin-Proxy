"""Genre computation (Phase 3 §7.1).

Maps a scene's Stash tags to Jellyfin `Genres` + residual `Tags`.
Three modes configured by `runtime.GENRE_MODE`:

  all_tags    every tag is a genre (minus system excludes)
  parent_tag  only direct children of GENRE_PARENT_TAG are genres
  top_n       the N most-populated tags in Stash are genres

The "allowed" set (parent-tag children or top-N tags) is fetched from
Stash once per 5 minutes. The system-exclude set (SERIES_TAG,
FAVORITE_TAG, TAG_GROUPS values, GENRE_PARENT_TAG itself, RATING:
prefix) is always applied on top of whatever the mode emits.

The filter-panel query reads `genre_allowed_names()` so the /Items/Filters
Genres dropdown matches what scenes actually surface as genres.
"""
import logging
import time
from typing import List, Optional, Set, Tuple

from stash_jellyfin_proxy import runtime
# stash_query is imported lazily inside the async helpers so that this
# module can be loaded in test environments without httpx installed.

logger = logging.getLogger("stash-jellyfin-proxy")


_ALLOWED_TTL_SECONDS = 300.0  # 5 min
# key = (mode, parent_tag, top_n) → (expires_at_monotonic, frozenset[str]|None)
# None value is an authoritative "no allow-list applies" for all_tags mode.
_allowed_cache: dict = {}
_warned_missing_parent: Set[str] = set()

# Sync-readable snapshot so format_jellyfin_item (sync) can apply the
# genre split without threading kwargs through 22 call sites. Async
# handlers refresh this at the top of their body.
_ALL_TAGS_SENTINEL = object()
_sync_snapshot = _ALL_TAGS_SENTINEL  # sentinel = "not populated yet"


def _system_excludes_lower() -> Set[str]:
    """Tags that must never appear as genres or in the residual Tags list
    regardless of mode (series marker, favorite marker, TAG_GROUPS values,
    genre_parent_tag itself). Lowercased for case-insensitive compare."""
    out: Set[str] = set()
    if runtime.SERIES_TAG:
        out.add(runtime.SERIES_TAG.strip().lower())
    if runtime.FAVORITE_TAG:
        out.add(runtime.FAVORITE_TAG.strip().lower())
    if runtime.GENRE_PARENT_TAG:
        out.add(runtime.GENRE_PARENT_TAG.strip().lower())
    for tg in runtime.TAG_GROUPS or []:
        out.add(tg.strip().lower())
    return out


def _is_rating_tag(name: str) -> bool:
    return (name or "").strip().upper().startswith("RATING:")


async def _fetch_parent_tag_children(parent_name: str) -> Optional[List[str]]:
    """Names of tags that are direct children of `parent_name` in Stash.
    Returns None if the parent tag doesn't exist (caller falls back to
    all_tags-style behaviour in that case)."""
    if not parent_name:
        return None
    # findTags with q= returns a prefix/substring match; then we filter by
    # exact (case-insensitive) name and walk its children list.
    q = """query FindParentTag($q: String!) {
        findTags(
            tag_filter: {name: {value: $q, modifier: EQUALS}},
            filter: {per_page: 5}
        ) {
            tags { id name children { id name } }
        }
    }"""
    try:
        from stash_jellyfin_proxy.stash.client import stash_query
        res = await stash_query(q, {"q": parent_name})
        tags = ((res or {}).get("data") or {}).get("findTags", {}).get("tags", []) or []
        target = None
        for t in tags:
            if (t.get("name") or "").strip().lower() == parent_name.strip().lower():
                target = t
                break
        if not target:
            if parent_name not in _warned_missing_parent:
                logger.warning(
                    f"genre_parent_tag '{parent_name}' not found in Stash; "
                    "parent_tag mode will emit no genres until it exists"
                )
                _warned_missing_parent.add(parent_name)
            return None
        return [c.get("name") for c in (target.get("children") or []) if c.get("name")]
    except Exception as e:
        logger.debug(f"parent-tag children lookup failed: {e}")
        return None


async def _fetch_top_n_tag_names(n: int) -> List[str]:
    """The top N most-populated tags (by scene_count), descending."""
    q = """query FindTopTags($n: Int!) {
        findTags(filter: {page: 1, per_page: $n, sort: "scenes_count", direction: DESC}) {
            tags { id name }
        }
    }"""
    try:
        from stash_jellyfin_proxy.stash.client import stash_query
        res = await stash_query(q, {"n": max(1, int(n))})
        tags = ((res or {}).get("data") or {}).get("findTags", {}).get("tags", []) or []
        return [t.get("name") for t in tags if t.get("name")]
    except Exception as e:
        logger.debug(f"top-N tag lookup failed: {e}")
        return []


async def genre_allowed_names() -> Optional[frozenset]:
    """Return the current allow-list of tag names (lowercase) that qualify
    as genres under the active mode. None means "every tag is allowed"
    (all_tags mode). Cached for 5 minutes."""
    mode = (runtime.GENRE_MODE or "parent_tag").lower()
    parent = (runtime.GENRE_PARENT_TAG or "").strip()
    top_n = int(runtime.GENRE_TOP_N or 25)
    key = (mode, parent, top_n)

    entry = _allowed_cache.get(key)
    now = time.monotonic()
    if entry is not None and entry[0] > now:
        return entry[1]

    if mode == "all_tags":
        allowed: Optional[frozenset] = None
    elif mode == "top_n":
        names = await _fetch_top_n_tag_names(top_n)
        allowed = frozenset(n.strip().lower() for n in names if n)
    else:  # parent_tag (default)
        names = await _fetch_parent_tag_children(parent)
        if names is None:
            # Parent tag missing — treat as empty allow-list so no tags
            # become genres. They still show up in `Tags`.
            allowed = frozenset()
        else:
            allowed = frozenset(n.strip().lower() for n in names if n)

    _allowed_cache[(mode, parent, top_n)] = (now + _ALLOWED_TTL_SECONDS, allowed)
    # Publish to the sync snapshot so format_jellyfin_item (sync) can
    # read the current allow-list without needing the keyword arg.
    global _sync_snapshot
    _sync_snapshot = allowed
    return allowed


_UNSET = object()


def compute_genres(
    scene_tags: List[str],
    allowed_lower=_UNSET,
) -> Tuple[List[str], List[str]]:
    """Split a scene's tag names into (genres, residual_tags).

    `allowed_lower` is the lowercase allow-list from `genre_allowed_names()`.
    Pass `None` for all_tags mode (every non-system tag becomes a genre).
    Omit the arg to read from the sync snapshot populated by async
    handlers at request entry.

    System excludes (SERIES/FAVORITE/TAG_GROUPS/GENRE_PARENT_TAG/RATING:)
    are stripped from BOTH output lists — they're plumbing, not user-facing.
    Tags are deduped case-insensitively; the genres list is sorted
    alphabetically (case-insensitive) so clients render a predictable
    order — Stash's per-scene tag order is effectively arbitrary.
    Residual tags preserve scene order (Stash already sorts them by
    tag usage upstream in some flows, and we want to leave that alone)."""
    if allowed_lower is _UNSET:
        snap = _sync_snapshot
        if snap is _ALL_TAGS_SENTINEL:
            allowed_lower = None  # snapshot not yet populated — safe fallback
        else:
            allowed_lower = snap

    if not scene_tags:
        return [], []

    excludes = _system_excludes_lower()
    genres: List[str] = []
    residual: List[str] = []
    seen_lower: Set[str] = set()

    for raw in scene_tags:
        name = (raw or "").strip()
        if not name:
            continue
        lc = name.lower()
        if lc in seen_lower:
            continue
        seen_lower.add(lc)
        if lc in excludes or _is_rating_tag(name):
            continue
        if allowed_lower is None or lc in allowed_lower:
            genres.append(name)
        else:
            residual.append(name)

    genres.sort(key=str.lower)
    return genres, residual


def invalidate_allowed_cache() -> None:
    """Clear the mode-allow-list cache (e.g. after config reload)."""
    global _sync_snapshot
    _allowed_cache.clear()
    _warned_missing_parent.clear()
    _sync_snapshot = _ALL_TAGS_SENTINEL
