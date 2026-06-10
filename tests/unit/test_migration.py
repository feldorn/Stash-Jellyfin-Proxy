"""Unit tests for the v1 → v2 config migration."""
import os

import pytest

from stash_jellyfin_proxy.config.loader import load_config
from stash_jellyfin_proxy.config.migration import (
    run_config_migration,
    CURRENT_CONFIG_VERSION as CURRENT,
    V2_DEFAULT_PLAYERS,
)


def _all_defaults_text():
    """All current V2_DEFAULT_PLAYERS sections rendered as config text — for
    seeding tests of v2 configs that should already be 'complete'."""
    out = []
    for name, body in V2_DEFAULT_PLAYERS:
        out.append(f"[{name}]")
        for k, v in body:
            out.append(f"{k} = {v}")
        out.append("")
    return "\n".join(out)


def _write(tmp_path, text):
    p = tmp_path / "stash_jellyfin_proxy.conf"
    p.write_text(text)
    return str(p)


def test_missing_file_is_noop(tmp_path):
    cfg, sections, performed, log = run_config_migration(
        str(tmp_path / "missing.conf"), {}, set(), {}
    )
    assert performed is False
    assert log == []


def test_already_migrated_with_all_defaults_is_noop(tmp_path):
    """A v2 config that already contains every current default player must
    not be touched on boot — same content + same mtime."""
    src = "STASH_URL = http://x\nCONFIG_VERSION = 2\n\n" + _all_defaults_text()
    path = _write(tmp_path, src)
    flat, defined, sections = load_config(path)
    mtime_before = os.path.getmtime(path)
    new_flat, new_sections, performed, log = run_config_migration(path, flat, defined, sections)
    mtime_after = os.path.getmtime(path)
    assert performed is False
    assert log == []
    assert mtime_after == mtime_before  # file untouched


def test_v2_heal_appends_missing_default_player(tmp_path):
    """A v2 config missing a default-player section gets that section
    appended on next boot, without bumping CONFIG_VERSION or marking the
    migration as performed (the schema didn't change)."""
    # Seed every default EXCEPT player.roku — simulates an install that
    # predates the release that added Roku to V2_DEFAULT_PLAYERS.
    defaults_minus_roku = "\n".join(
        f"[{name}]\n" + "\n".join(f"{k} = {v}" for k, v in body) + "\n"
        for name, body in V2_DEFAULT_PLAYERS if name != "player.roku"
    )
    src = "STASH_URL = http://x\nCONFIG_VERSION = 2\n\n" + defaults_minus_roku
    path = _write(tmp_path, src)
    flat, defined, sections = load_config(path)
    assert "player.roku" not in sections  # precondition

    new_flat, new_sections, performed, log = run_config_migration(path, flat, defined, sections)

    # Heal is not a schema migration.
    assert performed is False
    assert any("player.roku" in line for line in log)

    # The new section is reachable after the reload.
    assert "player.roku" in new_sections
    assert new_sections["player.roku"]["user_agent_match"] == "Roku"

    # And persisted on disk so the next boot is a no-op.
    on_disk = open(path).read()
    assert "[player.roku]" in on_disk


def test_v2_heal_is_idempotent(tmp_path):
    """Once the heal has appended a missing default, a second boot must
    leave the file untouched (no duplicate sections)."""
    src = "STASH_URL = http://x\nCONFIG_VERSION = 2\n"  # no player sections at all
    path = _write(tmp_path, src)
    flat, defined, sections = load_config(path)

    # First boot — heal appends every default player.
    run_config_migration(path, flat, defined, sections)
    flat2, defined2, sections2 = load_config(path)
    content_after_first = open(path).read()

    # Second boot — should be a no-op.
    mtime_before = os.path.getmtime(path)
    _, _, performed2, log2 = run_config_migration(path, flat2, defined2, sections2)
    mtime_after = os.path.getmtime(path)
    assert performed2 is False
    assert log2 == []
    assert mtime_after == mtime_before
    assert open(path).read() == content_after_first

    # And each default appears exactly once.
    for name, _ in V2_DEFAULT_PLAYERS:
        assert content_after_first.count(f"[{name}]") == 1


def test_v2_heal_preserves_user_customized_default_section(tmp_path):
    """If the user has hand-edited a default player section (e.g. changed
    Roku's poster_format), the heal must not overwrite their changes."""
    # User has player.roku but with a non-default poster_format.
    customized = "\n".join([
        "[player.roku]",
        "user_agent_match = Roku",
        "performer_type = BoxSet",
        "poster_format = portrait",  # ← user changed this from the default 'landscape'
        "",
    ])
    src = "STASH_URL = http://x\nCONFIG_VERSION = 2\n\n" + customized
    path = _write(tmp_path, src)
    flat, defined, sections = load_config(path)

    _new_flat, new_sections, performed, _log = run_config_migration(path, flat, defined, sections)

    assert performed is False
    # User's customization survived — not clobbered by the default.
    assert new_sections["player.roku"]["poster_format"] == "portrait"
    # And the file has exactly one [player.roku] block.
    assert open(path).read().count("[player.roku]") == 1


def test_v1_to_v2_adds_defaults_and_backs_up(tmp_path):
    src = """
# existing v1 config
STASH_URL = http://example:9999
STASH_API_KEY = abc
TAG_GROUPS = Gooning, JOI
"""
    path = _write(tmp_path, src)
    flat, defined, sections = load_config(path)

    new_flat, new_sections, performed, log = run_config_migration(path, flat, defined, sections)

    assert performed is True
    assert os.path.isfile(path + ".v1.bak")
    # Backup preserves original content
    assert "STASH_URL = http://example:9999" in (tmp_path / "stash_jellyfin_proxy.conf.v1.bak").read_text()

    # All v1 keys preserved
    assert new_flat["STASH_URL"] == "http://example:9999"
    assert new_flat["STASH_API_KEY"] == "abc"
    assert new_flat["TAG_GROUPS"] == "Gooning, JOI"

    # Version marker written
    assert int(new_flat["CONFIG_VERSION"]) == CURRENT

    # Every v2 default present with expected real values
    assert new_flat["genre_mode"] == "parent_tag"
    assert new_flat["series_tag"] == "SERIES"
    assert new_flat["sort_strip_articles"] == "The, A, An"
    assert new_flat["hero_source"] == "recent"
    assert new_flat["genre_filter_logic"] == "AND"

    # Default player profiles created
    assert "player.swiftfin" in new_sections
    assert new_sections["player.swiftfin"]["performer_type"] == "Person"
    assert new_sections["player.swiftfin"]["poster_format"] == "portrait"
    assert new_sections["player.infuse"]["poster_format"] == "landscape"
    assert new_sections["player.default"]["performer_type"] == "BoxSet"


def test_rerun_is_idempotent(tmp_path):
    """Running migration twice on a v1 file produces a v2 file on the
    first run and a no-op on the second."""
    path = _write(tmp_path, "STASH_URL = http://a\n")
    flat, defined, sections = load_config(path)
    _, _, performed1, _ = run_config_migration(path, flat, defined, sections)
    assert performed1 is True

    flat2, defined2, sections2 = load_config(path)
    _, _, performed2, log2 = run_config_migration(path, flat2, defined2, sections2)
    assert performed2 is False
    assert log2 == []


def test_user_defined_player_block_preserved(tmp_path):
    path = _write(tmp_path, """
STASH_URL = http://a

[player.swiftfin]
user_agent_match = CustomSwiftfin
performer_type = BoxSet
poster_format = landscape
""")
    flat, defined, sections = load_config(path)
    new_flat, new_sections, performed, _ = run_config_migration(path, flat, defined, sections)

    assert performed is True
    # User's customized profile wins
    assert new_sections["player.swiftfin"]["user_agent_match"] == "CustomSwiftfin"
    assert new_sections["player.swiftfin"]["performer_type"] == "BoxSet"
    assert new_sections["player.swiftfin"]["poster_format"] == "landscape"
    # Other default profiles still added
    assert "player.infuse" in new_sections
    assert "player.default" in new_sections


def test_existing_v2_key_keeps_user_value(tmp_path):
    """If a v2 key is already present in the file, migration must not
    overwrite it."""
    path = _write(tmp_path, """
STASH_URL = http://a
genre_mode = top_n
genre_top_n = 50
""")
    flat, defined, sections = load_config(path)
    new_flat, _, performed, _ = run_config_migration(path, flat, defined, sections)

    assert performed is True
    assert new_flat["genre_mode"] == "top_n"
    assert new_flat["genre_top_n"] == "50"
