"""Tests for CaseInsensitivePathMiddleware — path and query-string
case normalization.

Exercises the middleware directly through the ASGI protocol by
constructing a minimal `scope` dict and a stub `receive`/`send`, then
asserting the downstream app sees a normalized scope. Avoids Starlette
routing so the tests stay focused on the middleware's rewriting logic.
Async ASGI calls are driven via asyncio.run so the tests don't require
pytest-asyncio in the CI env.
"""
import asyncio

import pytest

from stash_jellyfin_proxy.middleware.paths import (
    CaseInsensitivePathMiddleware,
    _normalize_query_string,
)


@pytest.fixture(autouse=True)
def _rebuild_path_map():
    """Every test gets a fresh route table so leaking state between tests
    doesn't hide bugs. The routes cover the shapes we care about:
    static, template with one dynamic segment, template with a trailing
    slash, and a mixed-case template deep in a path."""

    class _Route:
        def __init__(self, path):
            self.path = path

    CaseInsensitivePathMiddleware.build_path_map([
        _Route("/Items"),
        _Route("/Users/{user_id}"),
        _Route("/Users/{user_id}/Items/{item_id}"),
        _Route("/Items/{item_id}/Images/Primary"),
        _Route("/Playlists/"),
        _Route("/Playlists/{playlist_id}/Items/"),
    ])
    yield


def _run(scope):
    """Send `scope` through the middleware and return the (possibly
    rewritten) scope the downstream app saw."""
    seen = {}

    async def downstream(s, receive, send):
        seen.update(s)

    async def _drive():
        mw = CaseInsensitivePathMiddleware(downstream)
        await mw(scope, None, None)

    asyncio.run(_drive())
    return seen


# --- path normalization (existing behavior, regression coverage) ---

def test_static_path_lowercase_gets_canonical_case():
    scope = {"type": "http", "path": "/items", "query_string": b""}
    result = _run(scope)
    assert result["path"] == "/Items"


def test_template_path_lowercase_gets_canonical_case():
    scope = {"type": "http", "path": "/users/abc/items/scene-11", "query_string": b""}
    result = _run(scope)
    assert result["path"] == "/Users/abc/Items/scene-11"


def test_template_deep_path_lowercase():
    """/items/scene-11/images/primary must resolve to
    /Items/{item_id}/Images/Primary — the fix arsfeld shipped in v7.3.0."""
    scope = {"type": "http", "path": "/items/scene-11/images/primary", "query_string": b""}
    result = _run(scope)
    assert result["path"] == "/Items/scene-11/Images/Primary"


def test_trailing_slash_on_static_is_stripped_as_fallback():
    """/items/?foo=bar (Roku appends a slash before the query) must still
    match /Items even though the exact match with the slash misses."""
    scope = {"type": "http", "path": "/items/", "query_string": b""}
    result = _run(scope)
    assert result["path"] == "/Items"


def test_route_registered_with_trailing_slash_still_matches():
    """/playlists/ must resolve to the registered /Playlists/ (with slash),
    not get its trailing slash stripped by the fallback."""
    scope = {"type": "http", "path": "/playlists/", "query_string": b""}
    result = _run(scope)
    assert result["path"] == "/Playlists/"


def test_unrecognized_path_is_left_alone():
    scope = {"type": "http", "path": "/no/such/route", "query_string": b""}
    result = _run(scope)
    assert result["path"] == "/no/such/route"


# --- query-string normalization (new in issue #27) ---

def test_qs_lowercase_names_canonicalized():
    """The bug report: Roku sends parentid=/startindex=/personids= etc.,
    which never hit .get('ParentId') / .get('StartIndex') / .get('PersonIds').
    Middleware must rewrite them."""
    qs = b"parentid=studio-5&startindex=60&personids=performer-64"
    out = _normalize_query_string(qs).decode("latin-1")
    assert "ParentId=studio-5" in out
    assert "StartIndex=60" in out
    assert "PersonIds=performer-64" in out


def test_qs_mixed_case_names_canonicalized():
    """`parentId` (camelCase, Infuse's spelling) must also normalize to
    the canonical `ParentId` — that's the whole point of one canonical
    reader spelling in the handlers."""
    qs = b"parentId=studio-5"
    out = _normalize_query_string(qs).decode("latin-1")
    assert "ParentId=studio-5" in out
    assert "parentId" not in out


def test_qs_canonical_case_is_a_noop():
    """Handler code passes canonical names; the middleware must not
    re-encode a query string when nothing needs changing (perf + cheap
    identity check for the fast path)."""
    qs = b"ParentId=studio-5&Limit=50"
    out = _normalize_query_string(qs)
    assert out is qs  # same object, no allocation


def test_qs_unknown_params_pass_through_unchanged():
    """Only param names in the canonical map get rewritten; anything
    else is left alone so custom / undocumented params still work."""
    qs = b"customThing=xyz&parentid=studio-5"
    out = _normalize_query_string(qs).decode("latin-1")
    assert "customThing=xyz" in out       # untouched
    assert "ParentId=studio-5" in out     # rewritten


def test_qs_empty_query_string_is_a_noop():
    assert _normalize_query_string(b"") == b""


def test_qs_values_are_not_touched():
    """Only names get canonicalized; case-sensitive values (like a
    filename or a hash) must survive unchanged."""
    qs = b"searchterm=Case-Sensitive_Value.mp4"
    out = _normalize_query_string(qs).decode("latin-1")
    assert "SearchTerm=Case-Sensitive_Value.mp4" in out


def test_scope_query_string_is_rewritten_end_to_end():
    """Through the full ASGI middleware, not just the helper — confirms
    the scope handoff wires up correctly."""
    scope = {"type": "http", "path": "/Items", "query_string": b"parentid=studio-5&startindex=60"}
    result = _run(scope)
    qs = result["query_string"].decode("latin-1")
    assert "ParentId=studio-5" in qs
    assert "StartIndex=60" in qs


def test_lowercase_path_and_query_normalized_together():
    """Roku's real request shape: fully-lowercase path AND fully-lowercase
    query parameter names. Both must be normalized in a single pass."""
    scope = {
        "type": "http",
        "path": "/items/",
        "query_string": b"parentid=studio-5&startindex=60&limit=20",
    }
    result = _run(scope)
    assert result["path"] == "/Items"
    qs = result["query_string"].decode("latin-1")
    assert "ParentId=studio-5" in qs
    assert "StartIndex=60" in qs
    assert "Limit=20" in qs
