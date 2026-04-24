"""v1 → v2 config migration.

Pure functions that inspect an existing config file and (if needed)
rewrite it with v2 defaults + player profile blocks added, preserving
every key the user had. Idempotent: re-running against a v2 file is a
no-op. Non-destructive: original is backed up to `<path>.v1.bak`
before write; on any mid-write failure we restore and surface the
error, so the proxy never fails to start because of a broken migration.
"""
import datetime
import os
import shutil
import sys

from .loader import load_config


CURRENT_CONFIG_VERSION = 2

# Canonical defaults for keys introduced after v1. Tuple (value, comment).
# Comment lines are written above the key for operator readability —
# the Web UI is the supported interface so most users never see this file.
V2_DEFAULT_FLAT = [
    ("CONFIG_VERSION", "2", "Schema version. Incremented when new keys need defaults written."),
    # Genre configuration
    ("genre_mode", "parent_tag", "Genre source: all_tags | parent_tag | top_n"),
    ("genre_parent_tag", "GENRE", "Parent tag whose direct children become genres (parent_tag mode)"),
    ("genre_top_n", "25", "Top-N tag count when genre_mode=top_n"),
    # Series detection
    ("series_tag", "SERIES", "Stash tag marking a studio as a TV Series"),
    ("series_episode_patterns",
     "S(\\d+)[:\\.]?E(\\d+), S(\\d+)\\s+E(\\d+), Season\\s*(\\d+).*?Episode\\s*(\\d+)",
     "Regex list (comma-separated) for parsing S/E from scene titles; first match wins"),
    # Sort configuration
    ("sort_strip_articles", "The, A, An", "Leading articles stripped for SortName"),
    ("scenes_default_sort", "DateCreated", "Default sort for Scenes library"),
    ("studios_default_sort", "SortName", "Default sort for Studios library"),
    ("performers_default_sort", "SortName", "Default sort for Performers library"),
    ("groups_default_sort", "SortName", "Default sort for Groups library"),
    ("tag_groups_default_sort", "PlayCount", "Default sort for TAG_GROUPS folders"),
    ("saved_filters_default_sort", "PlayCount", "Default sort for Saved Filter folders"),
    # Poster / image
    ("poster_crop_anchor", "center", "Anchor when cropping landscape to 2:3 portrait: center | left | right"),
    # Home hero
    ("hero_source", "recent", "Hero pool: recent | random | favorites | top_rated | recently_watched"),
    ("hero_min_rating", "75", "Minimum rating100 for hero when hero_source=top_rated"),
    # Search scope
    ("search_include_scenes", "true", "Include scenes in search results"),
    ("search_include_performers", "true", "Include performers in search results"),
    ("search_include_studios", "true", "Include studios in search results"),
    ("search_include_groups", "true", "Include groups in search results"),
    # Filter panel
    ("filter_tags_max", "50", "Max tags shown in filter panel per dimension"),
    ("genre_filter_logic", "AND", "Multi-select genre logic: AND | OR (standard Jellyfin = OR)"),
    ("filter_tags_walk_hierarchy", "true", "Expand selected tag to include Stash descendants"),
]

# Default player profile blocks. Swiftfin gets Person + portrait per design
# §3.2; Infuse gets BoxSet + landscape to preserve its current rendering;
# SenPlayer and default follow the safe-fallback BoxSet + portrait line.
V2_DEFAULT_PLAYERS = [
    ("player.swiftfin", [
        ("user_agent_match", "Swiftfin"),
        ("performer_type", "Person"),
        ("poster_format", "portrait"),
    ]),
    ("player.infuse", [
        ("user_agent_match", "Infuse"),
        ("performer_type", "BoxSet"),
        ("poster_format", "landscape"),
    ]),
    ("player.senplayer", [
        ("user_agent_match", "SenPlayer"),
        ("performer_type", "BoxSet"),
        ("poster_format", "portrait"),
    ]),
    ("player.default", [
        ("performer_type", "BoxSet"),
        ("poster_format", "portrait"),
    ]),
]

V2_FILE_HEADER = """\
# Stash-Jellyfin Proxy configuration — managed by the Web UI.
# To change settings, open http://<host>:<UI_PORT> and use the
# configuration tabs. Manual edits will be preserved but are not
# the supported interface.
#
# Schema version and migration timestamp recorded below.
"""


def _write_v2_config(path, existing_flat, existing_sections, preexisting_keys):
    """Serialize a full v2 config file. Returns a list of changes made."""
    changes = []
    lines = [V2_FILE_HEADER]
    lines.append(f"# Migrated: {datetime.datetime.now().isoformat(timespec='seconds')}\n")
    lines.append("")

    # Version marker first — identifies schema before any other keys.
    lines.append(f"CONFIG_VERSION = {CURRENT_CONFIG_VERSION}")
    lines.append("")

    lines.append("# ==== Preserved from previous config ====")
    for k in sorted(preexisting_keys):
        if k == "CONFIG_VERSION":
            continue
        v = existing_flat.get(k, "")
        if " " in str(v) or "#" in str(v):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    lines.append("")

    lines.append("# ==== v2 defaults ====")
    for key, value, comment in V2_DEFAULT_FLAT:
        if key == "CONFIG_VERSION":
            continue
        if key in preexisting_keys:
            continue
        lines.append(f"# {comment}")
        lines.append(f"{key} = {value}")
        lines.append("")
        changes.append(f"added default: {key} = {value}")

    # Player profile blocks come last (all flat keys must already be
    # written above, else they'd scope into the last section).
    lines.append("# ==== Player profiles ====")
    existing_player_names = set(existing_sections.keys())
    for section_name, body in existing_sections.items():
        if not section_name.startswith("player."):
            continue
        lines.append(f"[{section_name}]")
        for k, v in body.items():
            lines.append(f"{k} = {v}")
        lines.append("")
    for section_name, body in V2_DEFAULT_PLAYERS:
        if section_name in existing_player_names:
            continue
        lines.append(f"[{section_name}]")
        for k, v in body:
            lines.append(f"{k} = {v}")
        lines.append("")
        changes.append(f"added default profile: [{section_name}]")

    # Preserve any non-player sections the user has (forward compat).
    for section_name, body in existing_sections.items():
        if section_name.startswith("player."):
            continue
        lines.append(f"[{section_name}]")
        for k, v in body.items():
            lines.append(f"{k} = {v}")
        lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return changes


def run_config_migration(path, flat, defined, sections):
    """If the config at `path` is older than CURRENT_CONFIG_VERSION, back it
    up and rewrite with v2 defaults added.

    Returns (flat, sections, performed: bool, log: list[str]). Caller
    stores the updated dicts; nothing mutates module-level state here.
    """
    log = []
    try:
        current = int(flat.get("CONFIG_VERSION", "1"))
    except (TypeError, ValueError):
        current = 1

    if current >= CURRENT_CONFIG_VERSION:
        return flat, sections, False, log

    if not os.path.isfile(path):
        # Cold start with no file — nothing to migrate, caller handles
        # first-run config creation elsewhere.
        return flat, sections, False, log

    try:
        backup_path = path + ".v1.bak"
        if not os.path.isfile(backup_path):
            shutil.copy2(path, backup_path)
            log.append(f"backed up to {backup_path}")

        changes = _write_v2_config(path, flat, sections, defined)
        log.extend(changes)
        log.append(f"CONFIG_VERSION = {CURRENT_CONFIG_VERSION}")

        new_flat, new_defined, new_sections = load_config(path)
        return new_flat, new_sections, True, log
    except Exception as e:
        try:
            if os.path.isfile(path + ".v1.bak"):
                shutil.copy2(path + ".v1.bak", path)
                log.append("migration failed; restored from backup")
        except Exception:
            log.append("migration failed and backup restore also failed")
        log.append(f"error: {e}")
        print(f"Config migration failed: {e}", file=sys.stderr)
        for line in log:
            print(f"  [migrate] {line}", file=sys.stderr)
        return flat, sections, False, log
