"""Unit tests for mapping/genre.compute_genres.

Covers the synchronous split logic (system excludes, allow-list handling,
dedup, RATING: prefix). The async allow-list fetchers are exercised
separately via the characterization suite against real Stash."""
import pytest

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping import genre


@pytest.fixture(autouse=True)
def _reset_runtime():
    saved = (
        runtime.SERIES_TAG,
        runtime.FAVORITE_TAG,
        runtime.GENRE_PARENT_TAG,
        list(runtime.TAG_GROUPS),
    )
    runtime.SERIES_TAG = "Series"
    runtime.FAVORITE_TAG = "FAVORITE"
    runtime.GENRE_PARENT_TAG = "GENRE"
    runtime.TAG_GROUPS = ["Tit Worship", "JOI", "Gooning"]
    genre.invalidate_allowed_cache()
    yield
    (
        runtime.SERIES_TAG,
        runtime.FAVORITE_TAG,
        runtime.GENRE_PARENT_TAG,
        runtime.TAG_GROUPS,
    ) = saved
    genre.invalidate_allowed_cache()


# --- all_tags mode (allowed=None) -----------------------------------------

def test_all_tags_mode_every_non_system_tag_becomes_genre():
    # Stash's per-scene tag order is arbitrary; the proxy sorts genres
    # alphabetically (case-insensitive) so clients render a predictable list.
    genres, residual = genre.compute_genres(
        ["Masturbation", "POV", "Dirty Talk"], allowed_lower=None
    )
    assert genres == ["Dirty Talk", "Masturbation", "POV"]
    assert residual == []


def test_all_tags_mode_strips_system_excludes():
    genres, residual = genre.compute_genres(
        ["FAVORITE", "Series", "Masturbation", "GENRE", "JOI", "POV"],
        allowed_lower=None,
    )
    # FAVORITE_TAG, SERIES_TAG, GENRE_PARENT_TAG, TAG_GROUPS value (JOI) excluded.
    # Alphabetical sort means Masturbation sorts before POV anyway.
    assert genres == ["Masturbation", "POV"]
    assert residual == []


# --- parent_tag / top_n modes (explicit allow-list) -----------------------

def test_explicit_allow_list_splits_genres_and_residual():
    allowed = frozenset({"masturbation", "pov"})
    genres, residual = genre.compute_genres(
        ["Masturbation", "Dirty Talk", "POV", "Jerk Off Instruction"],
        allowed_lower=allowed,
    )
    assert genres == ["Masturbation", "POV"]
    assert residual == ["Dirty Talk", "Jerk Off Instruction"]


def test_empty_allow_list_means_no_genres():
    # parent_tag mode with a missing parent resolves to frozenset() — no
    # tag qualifies as a genre, but the residual still excludes system tags.
    genres, residual = genre.compute_genres(
        ["Masturbation", "FAVORITE", "POV", "Tit Worship"],
        allowed_lower=frozenset(),
    )
    assert genres == []
    assert residual == ["Masturbation", "POV"]


# --- RATING: prefix -------------------------------------------------------

def test_rating_prefix_stripped_from_both_outputs():
    genres, residual = genre.compute_genres(
        ["Masturbation", "RATING:85", "rating:72", "POV"],
        allowed_lower=None,
    )
    assert "RATING:85" not in genres and "RATING:85" not in residual
    assert "rating:72" not in genres and "rating:72" not in residual
    assert genres == ["Masturbation", "POV"]


# --- Dedup + whitespace + case --------------------------------------------

def test_case_insensitive_dedup_preserves_first_casing():
    genres, residual = genre.compute_genres(
        ["Masturbation", "masturbation", "MASTURBATION"], allowed_lower=None
    )
    assert genres == ["Masturbation"]
    assert residual == []


def test_whitespace_and_empty_tags_skipped():
    genres, residual = genre.compute_genres(
        ["  Masturbation  ", "", None, "   "], allowed_lower=None
    )
    assert genres == ["Masturbation"]


# --- Empty input ----------------------------------------------------------

def test_empty_tag_list_returns_empty():
    assert genre.compute_genres([], allowed_lower=None) == ([], [])
    assert genre.compute_genres(None, allowed_lower=None) == ([], [])


# --- Snapshot fallback (when arg omitted) ---------------------------------

def test_omitted_arg_reads_sync_snapshot(monkeypatch):
    # Populate the snapshot directly — simulating what genre_allowed_names
    # does after an async refresh.
    monkeypatch.setattr(genre, "_sync_snapshot", frozenset({"pov"}))
    genres, residual = genre.compute_genres(["Masturbation", "POV"])
    assert genres == ["POV"]
    assert residual == ["Masturbation"]


def test_unpopulated_snapshot_falls_back_to_all_tags(monkeypatch):
    # Before any async refresh happens, the snapshot is the sentinel and
    # compute_genres should safely emit every non-system tag as a genre.
    monkeypatch.setattr(genre, "_sync_snapshot", genre._ALL_TAGS_SENTINEL)
    genres, residual = genre.compute_genres(["Masturbation", "POV"])
    assert genres == ["Masturbation", "POV"]
    assert residual == []


# --- Runtime config sourcing ----------------------------------------------

def test_system_excludes_honour_runtime_values():
    runtime.FAVORITE_TAG = "MyFav"
    runtime.TAG_GROUPS = ["Hot", "Cool"]
    genres, residual = genre.compute_genres(
        ["MyFav", "Hot", "Cool", "Masturbation"], allowed_lower=None
    )
    assert genres == ["Masturbation"]
    assert residual == []
