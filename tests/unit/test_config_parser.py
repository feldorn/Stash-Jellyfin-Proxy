"""Unit tests for the INI-section-aware config parser."""
import pytest

from proxy.config.loader import load_config


def _write(tmp_path, text):
    p = tmp_path / "proxy.conf"
    p.write_text(text)
    return str(p)


def test_empty_file(tmp_path):
    cfg, keys, sections = load_config(_write(tmp_path, ""))
    assert cfg == {}
    assert keys == set()
    assert sections == {}


def test_missing_file(tmp_path):
    cfg, keys, sections = load_config(str(tmp_path / "nope.conf"))
    assert cfg == {}
    assert keys == set()
    assert sections == {}


def test_flat_keys_only_backward_compat(tmp_path):
    """Existing v1 configs (flat keys, no sections) must parse identically."""
    cfg, keys, sections = load_config(_write(tmp_path, """
# top-level keys
STASH_URL = https://stash-local.feldorn.com:9999
STASH_API_KEY = "xxxxxxxxxxx"
LOG_LEVEL=INFO
ENABLE_FILTERS = true
"""))
    assert cfg == {
        "STASH_URL": "https://stash-local.feldorn.com:9999",
        "STASH_API_KEY": "xxxxxxxxxxx",
        "LOG_LEVEL": "INFO",
        "ENABLE_FILTERS": "true",
    }
    assert "STASH_URL" in keys
    assert "LOG_LEVEL" in keys
    assert sections == {}


def test_sections_parse_into_nested_dict(tmp_path):
    cfg, keys, sections = load_config(_write(tmp_path, """
SERVER_NAME = Stash Dev

[player.swiftfin]
user_agent_match = Swiftfin
performer_type = Person
poster_format = portrait

[player.infuse]
user_agent_match = Infuse
performer_type = BoxSet
poster_format = landscape
"""))
    assert cfg == {"SERVER_NAME": "Stash Dev"}
    assert keys == {"SERVER_NAME"}
    assert sections == {
        "player.swiftfin": {
            "user_agent_match": "Swiftfin",
            "performer_type": "Person",
            "poster_format": "portrait",
        },
        "player.infuse": {
            "user_agent_match": "Infuse",
            "performer_type": "BoxSet",
            "poster_format": "landscape",
        },
    }


def test_flat_after_section_stays_in_section(tmp_path):
    """Once a [section] header is seen, all subsequent KEY=VALUE lines are
    scoped into that section until another header or EOF."""
    cfg, keys, sections = load_config(_write(tmp_path, """
STASH_URL = http://a
[player.default]
performer_type = BoxSet
poster_format = portrait
"""))
    assert cfg == {"STASH_URL": "http://a"}
    assert sections == {
        "player.default": {
            "performer_type": "BoxSet",
            "poster_format": "portrait",
        },
    }


def test_empty_section_header_resets_to_global(tmp_path):
    """`[]` (empty) is tolerated: resets scope to global. Guards against a
    malformed header silently swallowing subsequent flat keys."""
    cfg, keys, sections = load_config(_write(tmp_path, """
[player.x]
k = v
[]
OTHER_FLAT = y
"""))
    assert cfg == {"OTHER_FLAT": "y"}
    assert sections == {"player.x": {"k": "v"}}


def test_comments_and_blanks_ignored(tmp_path):
    cfg, keys, sections = load_config(_write(tmp_path, """
# a comment
   # indented comment

K = 1
    [player.a]
    # comment inside section
    x = 1
"""))
    assert cfg == {"K": "1"}
    assert sections == {"player.a": {"x": "1"}}


def test_quoted_values_strip_once(tmp_path):
    cfg, _, _ = load_config(_write(tmp_path, """
A = "hello"
B = 'world'
C = naked
"""))
    assert cfg == {"A": "hello", "B": "world", "C": "naked"}


def test_equals_sign_inside_value_preserved(tmp_path):
    """e.g. STASH_URL=http://x?a=b&c=d — only split on first '='."""
    cfg, _, _ = load_config(_write(tmp_path, """
URL=http://x?a=b&c=d
"""))
    assert cfg == {"URL": "http://x?a=b&c=d"}
