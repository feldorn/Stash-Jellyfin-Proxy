"""Folder-as-Series shape (issue #29) — studio- folders as a Series
with one Season per scene year, Episodes in date order."""
import asyncio
import json

import pytest

from stash_jellyfin_proxy.players.profiles import load_profiles

try:
    from stash_jellyfin_proxy.endpoints import folder_series as fs, views
except ImportError:  # pragma: no cover
    fs = views = None

needs_starlette = pytest.mark.skipif(fs is None, reason="starlette not installed")


def _run(coro):
    return asyncio.run(coro)


def test_folders_as_series_defaults():
    p = {x.name: x for x in load_profiles({
        "player.senplayer": {"user_agent_match": "SenPlayer"},
        "player.infuse": {"user_agent_match": "Infuse"},
        "player.other": {"user_agent_match": "X", "folders_as_series": "true"},
        "player.senplayer2": {"user_agent_match": "Y"},
    })}
    assert p["senplayer"].folders_as_series is True
    assert p["infuse"].folders_as_series is False
    assert p["other"].folders_as_series is True
    off = {x.name: x for x in load_profiles({
        "player.senplayer": {"user_agent_match": "SenPlayer", "folders_as_series": "false"}})}
    assert off["senplayer"].folders_as_series is False


@needs_starlette
def test_id_parsing():
    assert fs.is_folder_id("studio-12") and not fs.is_folder_id("tagitem-3")
    assert not fs.is_folder_id("studio-") and not fs.is_folder_id("tag-joi")
    assert not fs.is_folder_id("playlist-7")
    assert fs.parse_fseason("fseason-studio-12-2024") == ("studio-12", 2024)
    assert fs.parse_fseason("fseason-studio-3-0") == ("studio-3", 0)
    assert fs.parse_fseason("fseason-tagitem-3-0") is None
    assert fs.parse_fseason("fseason-tag-joi-2024") is None
    assert fs.parse_fseason("fseason-studio-12-x") is None
    assert fs.season_id("studio-12", 2024) == "fseason-studio-12-2024"


@needs_starlette
def test_as_series_shape_only_touches_folder_boxsets():
    studio = {"Id": "studio-5", "Type": "BoxSet", "CollectionType": "movies", "BackdropImageTags": []}
    fs.as_series_shape(studio)
    assert studio["Type"] == "Series" and "CollectionType" not in studio
    assert studio["BackdropImageTags"] == ["img"]
    for other in ({"Id": "filter-tags-1", "Type": "BoxSet"}, {"Id": "scene-1", "Type": "Movie"},
                  {"Id": "performer-2", "Type": "BoxSet"}):
        before = dict(other)
        assert fs.as_series_shape(other) == before


class _Req:
    def __init__(self, path_params=None, query_params=None):
        self.path_params = path_params or {}
        self.query_params = query_params or {}


@pytest.fixture
def fake_stash(monkeypatch):
    """Studio 5: two 2021 scenes (out of order), one 2023, one undated."""
    scenes = [
        {"id": "40", "title": "Late 2021", "date": "2021-09-01"},
        {"id": "10", "title": "Undated", "date": None},
        {"id": "30", "title": "Early 2021", "date": "2021-02-01"},
        {"id": "20", "title": "2023 one", "date": "2023-05-05"},
    ]
    seen = {}

    async def fake_query(q, variables=None):
        if "FolderSeriesScenes" in q:
            seen["filter"] = variables["f"]
            return {"data": {"findScenes": {"scenes": list(scenes)}}}
        if "FolderName" in q:
            key = "findStudio" if "findStudio" in q else "findTag"
            return {"data": {key: {"name": "Acme"}}}
        return {}

    async def no_genres():
        return set()

    async def no_warm():
        return None

    import stash_jellyfin_proxy.mapping.genre as genre
    monkeypatch.setattr(fs, "stash_query", fake_query)
    monkeypatch.setattr(genre, "genre_allowed_names", no_genres)
    monkeypatch.setattr(views, "_warm_genre_snapshot", no_warm)
    return seen


@needs_starlette
def test_seasons_by_year(fake_stash):
    out = _run(fs.folder_series_seasons("studio-5"))
    assert [(s["Name"], s["IndexNumber"], s["ChildCount"]) for s in out["Items"]] == [
        ("Undated", 0, 1), ("2021", 2021, 2), ("2023", 2023, 1)]
    assert all(s["SeriesId"] == "studio-5" for s in out["Items"])
    assert fake_stash["filter"] == {"studios": {"value": ["5"], "modifier": "INCLUDES"}}


@needs_starlette
def test_tagitem_is_not_a_series(fake_stash):
    tag = {"Id": "tagitem-9", "Type": "BoxSet", "CollectionType": "movies"}
    assert fs.as_series_shape(dict(tag)) == tag
    assert _run(fs.folder_series_seasons("tagitem-9"))["Items"] == []


@needs_starlette
def test_episodes_in_season_ordered_by_date(fake_stash):
    out = _run(fs.folder_series_episodes("studio-5", "fseason-studio-5-2021"))
    items = out["Items"]
    assert [i["Id"] for i in items] == ["scene-30", "scene-40"]
    assert [i["IndexNumber"] for i in items] == [1, 2]
    for i in items:
        assert i["Type"] == "Episode" and i["SeriesId"] == "studio-5"
        assert i["SeasonId"] == "fseason-studio-5-2021" and i["ParentIndexNumber"] == 2021
        assert i["SeriesName"] == "Acme"


@needs_starlette
def test_episodes_without_season_returns_all(fake_stash):
    out = _run(fs.folder_series_episodes("studio-5"))
    assert out["TotalRecordCount"] == 4


@needs_starlette
def test_episodes_reject_foreign_or_bad_season(fake_stash):
    assert _run(fs.folder_series_episodes("studio-5", "fseason-studio-6-2021"))["Items"] == []
    assert _run(fs.folder_series_episodes("studio-5", "garbage"))["Items"] == []


@needs_starlette
def test_season_item(fake_stash):
    s = _run(fs.folder_season_item("fseason-studio-5-2021"))
    assert s["Type"] == "Season" and s["ChildCount"] == 2 and s["Name"] == "2021"
    assert _run(fs.folder_season_item("fseason-studio-5-1999")) == {}


@needs_starlette
@pytest.mark.parametrize("path_id,query,expected", [
    ("studio-5", {"SeasonId": "fseason-studio-5-2021"}, 2),   # SenPlayer form
    ("fseason-studio-5-2023", {}, 1),                         # season in path
    ("studio-5", {}, 4),
])
def test_shows_episodes_routes_folder_ids(fake_stash, path_id, query, expected):
    r = _run(views.endpoint_shows_episodes(_Req({"series_id": path_id}, query)))
    assert json.loads(r.body)["TotalRecordCount"] == expected


@needs_starlette
def test_shows_seasons_and_nextup_route_folder_ids(fake_stash):
    r = _run(views.endpoint_shows_seasons(_Req({"series_id": "studio-5"})))
    assert len(json.loads(r.body)["Items"]) == 3
    r = _run(views.endpoint_shows_nextup(_Req(query_params={"SeriesId": "studio-5"})))
    assert json.loads(r.body)["Items"] == []
