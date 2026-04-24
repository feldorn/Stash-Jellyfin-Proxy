"""Unit tests for util.sort.sort_name_for."""
import pytest

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.util.sort import sort_name_for


@pytest.fixture(autouse=True)
def reset_articles():
    saved = list(runtime.SORT_STRIP_ARTICLES)
    runtime.SORT_STRIP_ARTICLES = ["The", "A", "An"]
    yield
    runtime.SORT_STRIP_ARTICLES = saved


def test_leading_the_is_stripped():
    assert sort_name_for("The Matrix") == "Matrix"


def test_leading_a_is_stripped():
    assert sort_name_for("A New Hope") == "New Hope"


def test_leading_an_is_stripped():
    assert sort_name_for("An Unexpected Journey") == "Unexpected Journey"


def test_only_leading_article_stripped_not_mid_title():
    assert sort_name_for("The Fault in The Stars") == "Fault in The Stars"


def test_case_insensitive_match_preserves_original_casing():
    assert sort_name_for("the matrix") == "matrix"
    assert sort_name_for("THE MATRIX") == "MATRIX"


def test_title_without_article_unchanged():
    assert sort_name_for("Inception") == "Inception"


def test_article_by_itself_is_kept():
    # "The" alone shouldn't strip to empty — fall back to original.
    assert sort_name_for("The") == "The"


def test_word_starting_with_article_but_not_an_article():
    # "Their" starts with "The" but it's not a leading article — keep it.
    assert sort_name_for("Their Story") == "Their Story"


def test_whitespace_trimmed():
    assert sort_name_for("  The Matrix  ") == "Matrix"


def test_empty_input():
    assert sort_name_for("") == ""
    assert sort_name_for(None) == ""


def test_empty_article_list_no_strip():
    runtime.SORT_STRIP_ARTICLES = []
    assert sort_name_for("The Matrix") == "The Matrix"


def test_custom_article_list():
    runtime.SORT_STRIP_ARTICLES = ["Le", "La", "Les"]
    assert sort_name_for("Le Film") == "Film"
    assert sort_name_for("La Belle") == "Belle"
    assert sort_name_for("The Movie") == "The Movie"  # no longer in list


def test_article_followed_by_punctuation():
    # "The-Matrix" with hyphen immediately after article.
    assert sort_name_for("The-Matrix") == "Matrix"
