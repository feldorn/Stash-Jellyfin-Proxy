"""Unit tests for the ID conversion helpers."""
from stash_jellyfin_proxy.util.ids import make_guid, extract_numeric_id, get_numeric_id


def test_make_guid_pads_and_formats():
    assert make_guid("123") == "00000000-0000-0000-0000-000000000123"
    assert make_guid(1) == "00000000-0000-0000-0000-000000000001"


def test_make_guid_exact_length_passes_through():
    # A 32-char hex input gets the dashes inserted (no padding change)
    assert make_guid("abcdef1234567890abcdef1234567890") == (
        "abcdef12-3456-7890-abcd-ef1234567890"
    )


def test_extract_numeric_id_guid_to_number():
    assert extract_numeric_id("00000000-0000-0000-0000-000000000123") == "123"
    assert extract_numeric_id("00000000-0000-0000-0000-000000000001") == "1"


def test_extract_numeric_id_all_zeros_yields_zero():
    assert extract_numeric_id("00000000-0000-0000-0000-000000000000") == "0"


def test_extract_numeric_id_passes_bare_numbers_through():
    assert extract_numeric_id("42") == "42"


def test_get_numeric_id_scene_and_studio_prefixes():
    assert get_numeric_id("scene-123") == "123"
    assert get_numeric_id("studio-456") == "456"


def test_get_numeric_id_preserves_legacy_behavior_for_other_prefixes():
    """Pre-0.6 get_numeric_id only stripped scene-/studio-. Other prefixed
    IDs (performer-, group-, tag-) fall through to extract_numeric_id which
    collapses dashes rather than peeling the prefix."""
    # performer-123 -> not scene/studio, has -, extract_numeric_id strips
    # dashes and lstrips zeros → "performer123"
    assert get_numeric_id("performer-123") == "performer123"
    assert get_numeric_id("group-42") == "group42"


def test_get_numeric_id_bare_input_is_unchanged():
    assert get_numeric_id("789") == "789"
