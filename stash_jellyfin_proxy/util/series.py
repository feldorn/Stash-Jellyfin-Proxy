"""Series/Episode title parsing.

The SERIES_EPISODE_PATTERNS runtime var is a comma-separated list of regex
patterns from the config (e.g. `S(\\d+)[:\\.]?E(\\d+), Season\\s*(\\d+).*?Episode\\s*(\\d+)`).
Each pattern must have exactly two capture groups — season and episode
numbers. First pattern to match wins.

Patterns compile lazily and are cached. Compile failures are logged once
and the bad pattern is skipped (never fatal)."""
import logging
import re
from typing import List, Optional, Tuple

from stash_jellyfin_proxy import runtime

logger = logging.getLogger("stash-jellyfin-proxy")

_compiled_cache: Optional[Tuple[str, List[re.Pattern]]] = None


def _compile_patterns() -> List[re.Pattern]:
    """Compile SERIES_EPISODE_PATTERNS, caching the result until the
    underlying config string changes (so config hot-reload takes effect)."""
    global _compiled_cache
    raw = getattr(runtime, "SERIES_EPISODE_PATTERNS", "") or ""
    if _compiled_cache is not None and _compiled_cache[0] == raw:
        return _compiled_cache[1]

    compiled: List[re.Pattern] = []
    for part in raw.split(","):
        pat = part.strip()
        if not pat:
            continue
        try:
            compiled.append(re.compile(pat, re.IGNORECASE))
        except re.error as e:
            logger.warning(f"Invalid series_episode pattern '{pat}': {e}")
    _compiled_cache = (raw, compiled)
    return compiled


def parse_episode(title: str) -> Optional[Tuple[int, int]]:
    """Return (season, episode) parsed from the title, or None if no
    configured pattern matches. Matches the first two capture groups
    of each pattern in order."""
    if not title:
        return None
    for pattern in _compile_patterns():
        m = pattern.search(title)
        if m and len(m.groups()) >= 2:
            try:
                return (int(m.group(1)), int(m.group(2)))
            except (ValueError, IndexError):
                continue
    return None


def episode_sort_key(title: str) -> Tuple[int, int]:
    """Sorting key for episodes. Scenes without parseable S/E sort to
    (0, 0) and rely on the caller to disambiguate via created_at."""
    parsed = parse_episode(title)
    return parsed if parsed else (0, 0)
