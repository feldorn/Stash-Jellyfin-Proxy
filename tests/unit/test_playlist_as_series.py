"""Playlist-as-Series shape (issue #29) — SenPlayer only builds a Next/Previous
queue for Episodes, so profiles with playlist_as_series get each playlist as a
one-season Series whose Episodes are the playlist scenes in stored order."""
import asyncio
import json

import pytest

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.players.profiles import load_profiles
from stash_jellyfin_proxy.state import playlist_order as po

try:  # endpoint/UI tests need starlette; profile tests don't
    from stash_jellyfin_proxy.endpoints import playlists, views
    from stash_jellyfin_proxy.mapping import image_policy
    from stash_jellyfin_proxy.ui import api as ui_api
except ImportError:  # pragma: no cover
    playlists = views = image_policy = ui_api = None

needs_starlette = pytest.mark.skipif(playlists is None, reason="starlette not installed")


def _run(coro):
    return asyncio.run(coro)


# ---- profile defaults ---------------------------------------------------

def _profiles(sections):
    return {p.name: p for p in load_profiles(sections)}


def test_senplayer_defaults_to_series_others_do_not():
    p = _profiles({
        "player.senplayer": {"user_agent_match": "SenPlayer"},
        "player.infuse": {"user_agent_match": "Infuse"},
        "player.swiftfin": {"user_agent_match": "Swiftfin"},
        "player.default": {},
    })
    assert p["senplayer"].playlist_as_series is True
    assert p["infuse"].playlist_as_series is False
    assert p["swiftfin"].playlist_as_series is False
    assert p["default"].playlist_as_series is False


def test_explicit_config_overrides_default():
    p = _profiles({
        "player.senplayer": {"user_agent_match": "SenPlayer", "playlist_as_series": "false"},
        "player.infuse": {"user_agent_match": "Infuse", "playlist_as_series": "true"},
    })
    assert p["senplayer"].playlist_as_series is False
    assert p["infuse"].playlist_as_series is True


# ---- item type policy ---------------------------------------------------

@needs_starlette
@pytest.mark.parametrize("native,as_series,expected", [
    (True, False, "Playlist"),
    (False, False, "BoxSet"),
    (False, True, "Series"),
    (True, True, "Series"),   # series wins over native
])
def test_playlist_item_type(monkeypatch, native, as_series, expected):
    from stash_jellyfin_proxy.players.profiles import Profile
    prof = Profile("x", "x", "BoxSet", "landscape", playlist_native=native,
                   playlist_as_series=as_series)
    monkeypatch.setattr(image_policy, "resolve_from_request", lambda _r: prof)
    assert image_policy.playlist_item_type(None) == expected


@needs_starlette
def test_series_playlist_item_shape():
    it = playlists._playlist_item({"id": "7", "name": "Mix", "scene_count": 3}, item_type="Series")
    assert it["Type"] == "Series" and it["Id"] == "playlist-7" and it["IsFolder"]
    assert "CollectionType" not in it and "MediaType" not in it


# ---- Shows endpoints ----------------------------------------------------

class _Req:
    def __init__(self, path_params=None, query_params=None):
        self.path_params = path_params or {}
        self.query_params = query_params or {}


@pytest.fixture
def fake_stash(monkeypatch, tmp_path):
    """Playlist tag 7 ("Road Trip") holds scenes 1..3; tag 8 is not a playlist.
    Scene 2 belongs to a SERIES studio so it already has Episode fields."""
    saved = runtime.CONFIG_FILE
    runtime.CONFIG_FILE = str(tmp_path / "stash_jellyfin_proxy.conf")
    po._reset_for_tests()
    scenes = {
        "1": {"id": "1", "title": "Alpha"},
        "2": {"id": "2", "title": "Show - S03:E05"},
        "3": {"id": "3", "title": "Gamma"},
    }

    async def fake_query(q, variables=None):
        if "PlaylistScenes" in q:
            return {"data": {"findScenes": {"scenes": [
                scenes[s] for s in sorted(scenes, key=int)]}}}
        if "PlaylistName" in q:
            return {"data": {"findTag": {"name": "Road Trip"}}}
        return {}

    async def is_playlist(tag_id):
        return tag_id == "7"

    async def no_genres():
        return set()

    real_format = playlists.format_jellyfin_item

    def fake_format(scene, parent_id=None):
        it = real_format(scene, parent_id=parent_id)
        if scene["id"] == "2":  # pretend SERIES-studio episode
            it.update({"Type": "Episode", "SeriesId": "series-50", "SeasonId": "season-50-3",
                       "ParentIndexNumber": 3, "IndexNumber": 5})
        return it

    import stash_jellyfin_proxy.mapping.genre as genre
    monkeypatch.setattr(playlists, "stash_query", fake_query)
    monkeypatch.setattr(playlists, "_is_playlist_tag", is_playlist)
    monkeypatch.setattr(playlists, "format_jellyfin_item", fake_format)
    monkeypatch.setattr(genre, "genre_allowed_names", no_genres)

    async def no_warm():
        return None
    monkeypatch.setattr(views, "_warm_genre_snapshot", no_warm)
    yield
    runtime.CONFIG_FILE = saved
    po._reset_for_tests()


@needs_starlette
def test_episodes_follow_stored_order_as_episodes(fake_stash):
    po.append("7", ["3", "1", "2"])
    out = _run(playlists.playlist_series_episodes("7"))
    items = out["Items"]
    assert [i["Id"] for i in items] == ["scene-3", "scene-1", "scene-2"]
    assert [i["IndexNumber"] for i in items] == [1, 2, 3]
    for i in items:
        assert i["Type"] == "Episode"
        assert i["SeriesId"] == "playlist-7" and i["SeasonId"] == "plseason-7"
        assert i["ParentId"] == "plseason-7" and i["ParentIndexNumber"] == 1
        assert i["SeriesName"] == "Road Trip"
    # A real-series scene is re-parented into the playlist series.
    assert items[2]["SeriesId"] == "playlist-7" and items[2]["IndexNumber"] == 3


@needs_starlette
def test_seasons_single_synthetic_season(fake_stash):
    out = _run(playlists.playlist_series_seasons("7"))
    assert out["TotalRecordCount"] == 1
    s = out["Items"][0]
    assert s["Id"] == "plseason-7" and s["Type"] == "Season"
    assert s["SeriesId"] == "playlist-7" and s["ChildCount"] == 3


@needs_starlette
def test_non_playlist_tag_is_empty(fake_stash):
    assert _run(playlists.playlist_series_episodes("8"))["Items"] == []
    assert _run(playlists.playlist_series_seasons("8"))["Items"] == []
    assert _run(playlists.playlist_season_item("8")) == {}


@needs_starlette
def test_empty_playlist(fake_stash, monkeypatch):
    async def empty(q, variables=None):
        if "PlaylistName" in q:
            return {"data": {"findTag": {"name": "Empty"}}}
        return {"data": {"findScenes": {"scenes": []}}}
    monkeypatch.setattr(playlists, "stash_query", empty)
    assert _run(playlists.playlist_series_episodes("7"))["Items"] == []
    assert _run(playlists.playlist_series_seasons("7"))["Items"][0]["ChildCount"] == 0


@needs_starlette
@pytest.mark.parametrize("path_id,query", [
    ("playlist-7", {"SeasonId": "plseason-7"}),   # SenPlayer
    ("playlist-7", {}),
    ("plseason-7", {}),                           # season id in the path
])
def test_shows_episodes_routes_playlist_ids(fake_stash, path_id, query):
    r = _run(views.endpoint_shows_episodes(_Req({"series_id": path_id}, query)))
    body = json.loads(r.body)
    assert body["TotalRecordCount"] == 3
    assert {i["SeasonId"] for i in body["Items"]} == {"plseason-7"}


@needs_starlette
def test_shows_seasons_routes_playlist_ids(fake_stash):
    r = _run(views.endpoint_shows_seasons(_Req({"series_id": "playlist-7"})))
    assert [s["Id"] for s in json.loads(r.body)["Items"]] == ["plseason-7"]


@needs_starlette
def test_nextup_for_playlist_series_is_empty(fake_stash):
    r = _run(views.endpoint_shows_nextup(_Req(query_params={"SeriesId": "playlist-7", "Limit": "1"})))
    assert json.loads(r.body) == {"Items": [], "TotalRecordCount": 0}


@needs_starlette
def test_plseason_tag_id_parsing():
    assert playlists.plseason_tag_id("plseason-12") == "12"
    assert playlists.plseason_tag_id("plseason-x") == ""
    assert playlists.plseason_tag_id("season-1-2") == ""


# ---- Web UI profile save keeps playlist keys ----------------------------

_CONF = """SERVER_NAME = "x"

[player.infuse]
user_agent_match = Infuse
playlist_native = true

[player.senplayer]
user_agent_match = SenPlayer
playlist_as_series = false
performer_type = BoxSet
poster_format = portrait

[player.default]
performer_type = BoxSet
poster_format = landscape
"""


class _JsonReq:
    method = "POST"

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def ui_conf(tmp_path, monkeypatch):
    from stash_jellyfin_proxy.config.loader import load_config
    path = tmp_path / "stash_jellyfin_proxy.conf"
    path.write_text(_CONF)
    saved = (runtime.CONFIG_FILE, runtime.config_sections, runtime.PLAYER_PROFILES)
    runtime.CONFIG_FILE = str(path)
    _cfg, _keys, sections = load_config(str(path))
    runtime.config_sections = sections
    yield path
    runtime.CONFIG_FILE, runtime.config_sections, runtime.PLAYER_PROFILES = saved


@needs_starlette
def test_saving_one_profile_keeps_other_profiles_playlist_keys(ui_conf):
    r = _run(ui_api.ui_api_players_save_profile(_JsonReq({
        "name": "infuse", "userAgentMatch": "Infuse",
        "performerType": "BoxSet", "posterFormat": "landscape",
    })))
    assert r.status_code == 200
    text = ui_conf.read_text()
    assert "playlist_as_series = false" in text   # senplayer override survived
    assert "playlist_native = true" in text       # infuse key survived (merge)


@needs_starlette
def test_saving_playlist_mode_writes_keys_and_reloads(ui_conf):
    _run(ui_api.ui_api_players_save_profile(_JsonReq({
        "name": "senplayer", "userAgentMatch": "SenPlayer",
        "performerType": "BoxSet", "posterFormat": "portrait", "playlistMode": "series",
    })))
    profiles = {p.name: p for p in runtime.PLAYER_PROFILES}
    assert profiles["senplayer"].playlist_as_series is True
    assert ui_api._profile_dict(profiles["senplayer"])["playlistMode"] == "series"
    assert ui_api._profile_dict(profiles["infuse"])["playlistMode"] == "native"


# ---- profile matcher picks up Web UI edits ------------------------------

def test_matcher_cache_follows_profile_reload(monkeypatch):
    from stash_jellyfin_proxy.players import matcher
    monkeypatch.setattr(matcher, "_load_log_once", lambda: None)
    monkeypatch.setattr(matcher, "_save_log", lambda: None)
    monkeypatch.setattr(matcher, "_ua_log", {})
    monkeypatch.setattr(matcher, "_ua_cache", {})
    saved = runtime.PLAYER_PROFILES
    try:
        runtime.PLAYER_PROFILES = load_profiles({"player.senplayer": {"user_agent_match": "SenPlayer"}})
        assert matcher.resolve_profile("SenPlayer/6.2.3").playlist_as_series is True
        runtime.PLAYER_PROFILES = load_profiles({"player.senplayer": {
            "user_agent_match": "SenPlayer", "playlist_as_series": "false"}})
        assert matcher.resolve_profile("SenPlayer/6.2.3").playlist_as_series is False
    finally:
        runtime.PLAYER_PROFILES = saved


# ---- playlist item carries real-server fields (Infuse Recent Playlists) --

@needs_starlette
def test_playlist_item_has_real_server_fields():
    it = playlists._playlist_item({"id": "7", "name": "Mix", "scene_count": 2,
                                   "created_at": "2026-11-17T15:41:43-06:00",
                                   "updated_at": "2026-11-17T16:00:00Z"})
    assert it["DateCreated"] == "2026-11-17T21:41:43.0000000Z"
    assert it["DateLastMediaAdded"] == "2026-11-17T16:00:00.0000000Z"
    assert it["ParentId"] == "root-playlists" and it["SortName"] == "mix"
    assert it["UserData"]["UnplayedItemCount"] == 2 and it["UserData"]["Key"] == "playlist-7"


@needs_starlette
def test_playlist_item_tolerates_missing_dates():
    it = playlists._playlist_item({"id": "7", "name": "Mix", "created_at": "garbage"})
    assert "DateCreated" not in it and "DateLastMediaAdded" not in it
