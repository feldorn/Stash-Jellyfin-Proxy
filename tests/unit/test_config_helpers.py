"""Unit tests for the pure config-value helpers."""
import uuid

import pytest

from stash_jellyfin_proxy.config.helpers import (
    parse_bool,
    normalize_path,
    normalize_server_id,
    generate_server_id,
)


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("TRUE", True), ("True", True),
    ("yes", True), ("1", True), ("on", True),
    ("false", False), ("no", False), ("0", False), ("off", False),
    ("", False), ("garbage", False),
])
def test_parse_bool_string_inputs(value, expected):
    assert parse_bool(value) is expected


def test_parse_bool_passes_bool_through():
    assert parse_bool(True) is True
    assert parse_bool(False) is False


def test_parse_bool_other_types_return_default():
    assert parse_bool(None) is True          # default
    assert parse_bool(None, default=False) is False
    assert parse_bool(123, default=True) is True


@pytest.mark.parametrize("value,expected", [
    ("/graphql", "/graphql"),
    ("graphql", "/graphql"),
    ("/graphql/", "/graphql"),
    ("/graphql-local/", "/graphql-local"),
    ("", "/graphql"),
    ("   ", "/graphql"),
])
def test_normalize_path(value, expected):
    assert normalize_path(value) == expected


def test_normalize_path_custom_default():
    assert normalize_path("", default="/custom") == "/custom"


def test_normalize_server_id_converts_dashless_hex_to_uuid():
    dashless = "efbf7f031234567890abcdef12345678"
    assert normalize_server_id(dashless) == "efbf7f03-1234-5678-90ab-cdef12345678"


def test_normalize_server_id_passes_valid_uuid_through():
    valid = "efbf7f03-1234-5678-90ab-cdef12345678"
    assert normalize_server_id(valid) == valid


def test_normalize_server_id_passes_non_hex_through():
    """Bad input should not raise — Web UI surfaces the issue instead."""
    assert normalize_server_id("not-a-uuid") == "not-a-uuid"


def test_generate_server_id_returns_valid_uuid_string():
    out = generate_server_id()
    # Parses as UUID round-trip
    assert str(uuid.UUID(out)) == out
