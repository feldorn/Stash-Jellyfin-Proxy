"""Persisted playlist ordering (issue #29) — state/playlist_order + the
Move / list endpoints wired through it."""
import asyncio
import json
import os

import pytest

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.state import playlist_order as po

try:  # endpoint tests need starlette; state tests don't
    from stash_jellyfin_proxy.endpoints import playlists
except ImportError:  # pragma: no cover
    playlists = None

needs_starlette = pytest.mark.skipif(playlists is None, reason="starlette not installed")


@pytest.fixture
def store(tmp_path):
    saved = runtime.CONFIG_FILE
    runtime.CONFIG_FILE = str(tmp_path / "stash_jellyfin_proxy.conf")
    po._reset_for_tests()
    yield tmp_path / "playlist_order.json"
    runtime.CONFIG_FILE = saved
    po._reset_for_tests()


# ---- state module -------------------------------------------------------

def test_unknown_playlist_orders_by_scene_id(store):
    assert po.apply_order("7", ["30", "4", "12"]) == ["4", "12", "30"]


def test_append_order_wins_and_extras_follow(store):
    po.append("7", ["30", "4"])
    # "12" was tagged directly in Stash — goes after stored entries.
    assert po.apply_order("7", ["4", "12", "30"]) == ["30", "4", "12"]


def test_stale_ids_dropped(store):
    po.append("7", ["1", "2", "3"])
    assert po.apply_order("7", ["3", "1"]) == ["1", "3"]


def test_reappend_moves_to_end(store):
    po.append("7", ["1", "2", "3"])
    po.append("7", ["1"])
    assert po.apply_order("7", ["1", "2", "3"]) == ["2", "3", "1"]


def test_move_to_front_middle_end_and_clamp(store):
    members = ["1", "2", "3", "4"]
    po.append("7", members)
    assert po.move("7", members, "4", 0)
    assert po.apply_order("7", members) == ["4", "1", "2", "3"]
    assert po.move("7", members, "4", 2)
    assert po.apply_order("7", members) == ["1", "2", "4", "3"]
    assert po.move("7", members, "1", 99)
    assert po.apply_order("7", members) == ["2", "4", "3", "1"]


def test_move_materializes_unstored_members(store):
    # Nothing stored yet: Move must persist the full reconciled order.
    assert po.move("7", ["5", "9", "2"], "9", 0)
    assert po.apply_order("7", ["2", "5", "9"]) == ["9", "2", "5"]


def test_move_rejects_non_member(store):
    assert po.move("7", ["1", "2"], "3", 0) is False


def test_remove_and_forget(store):
    po.append("7", ["1", "2", "3"])
    po.remove("7", ["2"])
    po.append("8", ["9"])
    po.forget("7")
    data = json.loads(store.read_text())
    assert "7" not in data and data["8"] == ["9"]


def test_persists_across_reload(store):
    po.append("7", ["3", "1", "2"])
    po._reset_for_tests()
    assert po.apply_order("7", ["1", "2", "3"]) == ["3", "1", "2"]


def test_corrupt_file_is_ignored(store):
    store.write_text("{not json")
    assert po.apply_order("7", ["2", "1"]) == ["1", "2"]


# ---- endpoints ----------------------------------------------------------

class _Req:
    def __init__(self, path_params=None, query_params=None):
        self.path_params = path_params or {}
        self.query_params = query_params or {}


@pytest.fixture
def fake_stash(monkeypatch, store):
    """Playlist tag 7 holds scenes 1..4; every tag counts as a playlist."""
    members = {"1", "2", "3", "4"}

    async def fake_query(q, variables=None):
        if "PlaylistScenes" in q:
            return {"data": {"findScenes": {"scenes": [
                {"id": sid} for sid in sorted(members, key=int)
            ]}}}
        return {}

    async def yes(_tag_id):
        return True

    monkeypatch.setattr(playlists, "stash_query", fake_query)
    monkeypatch.setattr(playlists, "_is_playlist_tag", yes)
    return members


def _run(coro):
    return asyncio.run(coro)


@needs_starlette
def test_move_endpoint_reorders_get_playlist(fake_stash):
    r = _run(playlists.endpoint_playlist_move_item(_Req(
        {"playlist_id": "playlist-7", "item_id": "scene-3", "new_index": "0"})))
    assert r.status_code == 204
    r = _run(playlists.endpoint_get_playlist(_Req({"playlist_id": "playlist-7"})))
    assert json.loads(r.body)["ItemIds"] == ["scene-3", "scene-1", "scene-2", "scene-4"]


@needs_starlette
def test_move_endpoint_rejects_bad_input(fake_stash):
    r = _run(playlists.endpoint_playlist_move_item(_Req(
        {"playlist_id": "playlist-7", "item_id": "scene-99", "new_index": "0"})))
    assert r.status_code == 404
    r = _run(playlists.endpoint_playlist_move_item(_Req(
        {"playlist_id": "playlist-7", "item_id": "scene-1", "new_index": "x"})))
    assert r.status_code == 400


@needs_starlette
def test_ordered_scenes_follow_store_and_drop_untagged(fake_stash):
    po.append("7", ["4", "99", "2"])  # 99 no longer tagged
    scenes = _run(playlists.ordered_playlist_scenes("7"))
    assert [s["id"] for s in scenes] == ["4", "2", "1", "3"]
