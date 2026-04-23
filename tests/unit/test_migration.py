"""Unit tests for the v1 → v2 config migration.

Executes only the migration helpers by extracting them from the single-file
proxy, so tests don't depend on running the whole Starlette app.
"""
import ast
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROXY_SRC_PATH = REPO_ROOT / "stash_jellyfin_proxy.py"
SOURCE = PROXY_SRC_PATH.read_text()


def _extract():
    """Pull run_config_migration, _write_v2_config and their module-level
    state from the monolith (where they still live pre-extraction) into a
    self-contained namespace. load_config is now its own module so we just
    import it."""
    from proxy.config.loader import load_config as _load_config
    tree = ast.parse(SOURCE)
    wanted_funcs = {"run_config_migration", "_write_v2_config"}
    wanted_assigns = {
        "CURRENT_CONFIG_VERSION", "_V2_DEFAULT_FLAT", "_V2_DEFAULT_PLAYERS",
        "_V2_FILE_HEADER", "MIGRATION_PERFORMED", "MIGRATION_LOG",
    }
    parts = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            parts.append(ast.get_source_segment(SOURCE, node))
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(name in wanted_assigns for name in targets):
                parts.append(ast.get_source_segment(SOURCE, node))
    ns = {
        "os": os,
        "sys": sys,
        "datetime": __import__("datetime"),
        "load_config": _load_config,  # migration helpers call this internally
    }
    exec("\n\n".join(parts), ns)
    ns["load_config"] = _load_config
    return ns


MIGRATION = _extract()
load_config = MIGRATION["load_config"]
run_config_migration = MIGRATION["run_config_migration"]
CURRENT = MIGRATION["CURRENT_CONFIG_VERSION"]


def _write(tmp_path, text):
    p = tmp_path / "proxy.conf"
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
    assert "STASH_URL = http://example:9999" in (tmp_path / "proxy.conf.v1.bak").read_text()

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
