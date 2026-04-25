"""Player profiles — config-driven per-client rendering behavior.

Profiles come from [player.<name>] sections in the config. The migration
writes sensible defaults for swiftfin / infuse / senplayer / default.
Users edit via the Web UI; the file is the persistence format.

A Profile is the resolved view — immutable per-request, cached per-UA.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Profile:
    name: str                       # section suffix, e.g. "swiftfin"
    user_agent_match: str           # substring matched against UA (empty for default)
    performer_type: str             # "Person" or "BoxSet"
    poster_format: str              # "portrait" or "landscape"
    # Playlist rendering mode. True = native shape (CollectionType=playlists,
    # Type=Playlist) — Infuse and the Jellyfin web client get full create/
    # edit/delete UI. False = compat shape (CollectionType=movies, Type=BoxSet)
    # — Swiftfin and SenPlayer can browse + play but not manage, since their
    # UI doesn't render the native Playlist types.
    playlist_native: bool = True


# Profiles whose UI doesn't render native Playlist items — exposed in compat
# mode (movies/BoxSet) so users can still browse and play. Used as a fallback
# when the config doesn't pin `playlist_native` explicitly.
#
#   Swiftfin:   CollectionType.supportedCases excludes .playlists,
#               BaseItemKind.supportedCases excludes .playlist
#               (jellyfin/Swiftfin main, 2026; tracking issue #609 still open).
#               Browses fine when shaped as movies/BoxSet.
#   SenPlayer:  closed-source; no Jellyfin-playlist UI per App Store / forum.
_PLAYLISTS_NON_NATIVE_PROFILES = frozenset({"swiftfin", "senplayer"})


# Fallback if [player.default] is missing from config (migration failure, etc.).
_HARDCODED_DEFAULT = Profile(
    name="default",
    user_agent_match="",
    performer_type="BoxSet",
    poster_format="landscape",
    playlist_native=True,
)


def _parse_bool(raw: str, default: bool) -> bool:
    """Same truthy parse the config helpers use, inlined to avoid a circular
    import (config.helpers imports nothing from players)."""
    if raw is None:
        return default
    return str(raw).strip().lower() in ("true", "yes", "1", "on")


def load_profiles(sections: Dict[str, Dict[str, str]]) -> List[Profile]:
    """Load profiles from a dict-of-dicts (as produced by the section-aware
    config loader). Returns a list with [player.default] last so matching
    iterates specific profiles first and falls through.

    Missing fields fall back to the default profile's values. Missing
    [player.default] is tolerated — we synthesize one — but logged by the
    caller."""
    default_cfg = sections.get("player.default", {})
    default = Profile(
        name="default",
        user_agent_match="",
        performer_type=default_cfg.get("performer_type", _HARDCODED_DEFAULT.performer_type),
        poster_format=default_cfg.get("poster_format", _HARDCODED_DEFAULT.poster_format),
        playlist_native=_parse_bool(
            default_cfg.get("playlist_native"), _HARDCODED_DEFAULT.playlist_native
        ),
    )

    profiles: List[Profile] = []
    for section_name, body in sections.items():
        if not section_name.startswith("player.") or section_name == "player.default":
            continue
        name = section_name[len("player."):]
        # Default playlist_native from config if explicitly set; otherwise
        # compat-mode for known clients without playlist UI, native for the rest.
        if "playlist_native" in body:
            playlist_native = _parse_bool(body.get("playlist_native"), True)
        else:
            playlist_native = name not in _PLAYLISTS_NON_NATIVE_PROFILES
        profiles.append(Profile(
            name=name,
            user_agent_match=body.get("user_agent_match", ""),
            performer_type=body.get("performer_type", default.performer_type),
            poster_format=body.get("poster_format", default.poster_format),
            playlist_native=playlist_native,
        ))
    profiles.append(default)
    return profiles


def hardcoded_default() -> Profile:
    """Accessor for the hardcoded fallback — used when config has no
    [player.default] and we need a last-resort profile."""
    return _HARDCODED_DEFAULT
