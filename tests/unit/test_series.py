"""Unit tests for `util.series.parse_episode`.

The parser consumes the comma-separated SERIES_EPISODE_PATTERNS config
string, compiling each pattern on first use and caching until the config
changes. Tests poke runtime directly to control which patterns are active."""
import pytest

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.util import series


DEFAULT_PATTERNS = (
    r"S(\d+)[:\.]?E(\d+), "
    r"S(\d+)\s+E(\d+), "
    r"Season\s*(\d+).*?Episode\s*(\d+)"
)


@pytest.fixture(autouse=True)
def reset_patterns():
    """Reset the compile cache between tests so each test picks up its
    own SERIES_EPISODE_PATTERNS value."""
    saved = runtime.SERIES_EPISODE_PATTERNS
    series._compiled_cache = None
    runtime.SERIES_EPISODE_PATTERNS = DEFAULT_PATTERNS
    yield
    runtime.SERIES_EPISODE_PATTERNS = saved
    series._compiled_cache = None


def test_colon_separator():
    # NF Busty canonical form
    assert series.parse_episode("Scene Title S23:E8") == (23, 8)
    assert series.parse_episode("S23:E8 - Scene Title") == (23, 8)


def test_dot_separator():
    assert series.parse_episode("Scene Title S2.E5") == (2, 5)


def test_no_separator():
    # S1E5, SxxExx compact form
    assert series.parse_episode("Scene S1E5 prefix") == (1, 5)


def test_space_separator():
    assert series.parse_episode("My Show S07 E03") == (7, 3)


def test_long_form():
    assert series.parse_episode("Season 2 Episode 11: Revenge") == (2, 11)
    assert series.parse_episode("— Season 10, Episode 4 —") == (10, 4)


def test_case_insensitive():
    assert series.parse_episode("s1:e5") == (1, 5)
    assert series.parse_episode("season 3 episode 2") == (3, 2)


def test_first_match_wins():
    # Both S1E5 and "Season 2 Episode 9" appear — first configured pattern
    # (S…E…) should win.
    assert series.parse_episode("S1E5 — Season 2 Episode 9") == (1, 5)


def test_no_match_returns_none():
    assert series.parse_episode("A regular scene title") is None
    assert series.parse_episode("") is None
    assert series.parse_episode(None) is None


def test_double_digit_numbers():
    assert series.parse_episode("S23:E17") == (23, 17)
    assert series.parse_episode("Season 100 Episode 250") == (100, 250)


def test_empty_pattern_config():
    runtime.SERIES_EPISODE_PATTERNS = ""
    series._compiled_cache = None
    assert series.parse_episode("S1:E5") is None


def test_invalid_pattern_skipped_not_fatal():
    # Bad regex in middle of list — good patterns still work
    runtime.SERIES_EPISODE_PATTERNS = r"[unclosed, S(\d+):E(\d+)"
    series._compiled_cache = None
    assert series.parse_episode("S4:E7") == (4, 7)


def test_compile_cache_invalidates_on_config_change():
    runtime.SERIES_EPISODE_PATTERNS = r"S(\d+):E(\d+)"
    series._compiled_cache = None
    assert series.parse_episode("S1:E1") == (1, 1)
    assert series.parse_episode("Season 2 Episode 3") is None

    # Swap patterns — cache should rebuild
    runtime.SERIES_EPISODE_PATTERNS = r"Season\s*(\d+).*?Episode\s*(\d+)"
    assert series.parse_episode("Season 2 Episode 3") == (2, 3)
    assert series.parse_episode("S1:E1") is None


def test_episode_sort_key_parsed():
    assert series.episode_sort_key("S3:E7") == (3, 7)


def test_episode_sort_key_unparsed_falls_to_zero():
    assert series.episode_sort_key("Random scene") == (0, 0)
    assert series.episode_sort_key("") == (0, 0)
