"""Unit tests for the v1 → v2 config migration."""
import os

import pytest

from stash_jellyfin_proxy.config.loader import load_config
from stash_jellyfin_proxy.config.migration import (
    run_config_migration,
    CURRENT_CONFIG_VERSION as CURRENT,
)


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


def test_already_migrated_is_noop(tmp_path):
    path = _write(tmp_path, "STASH_URL = http://x\nCONFIG_VERSION = 2\n")
    flat, defined, sections = load_config(path)
    mtime_before = os.path.getmtime(path)
    new_flat, new_sections, performed, log = run_config_migration(path, flat, defined, sections)
    mtime_after = os.path.getmtime(path)
    assert performed is False
    assert log == []
    assert mtime_after == mtime_before  # file untouched


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
