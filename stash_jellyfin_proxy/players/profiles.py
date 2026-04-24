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


# Fallback if [player.default] is missing from config (migration failure, etc.).
_HARDCODED_DEFAULT = Profile(
    name="default",
    user_agent_match="",
    performer_type="BoxSet",
    poster_format="landscape",
)


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
    )

    profiles: List[Profile] = []
    for section_name, body in sections.items():
        if not section_name.startswith("player.") or section_name == "player.default":
            continue
        profiles.append(Profile(
            name=section_name[len("player."):],
            user_agent_match=body.get("user_agent_match", ""),
            performer_type=body.get("performer_type", default.performer_type),
            poster_format=body.get("poster_format", default.poster_format),
        ))
    profiles.append(default)
    return profiles


def hardcoded_default() -> Profile:
    """Accessor for the hardcoded fallback — used when config has no
    [player.default] and we need a last-resort profile."""
    return _HARDCODED_DEFAULT
