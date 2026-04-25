"""Home-tab and library-browse endpoints — Views, VirtualFolders,
Next Up, Latest, Resume, and the Sessions scrobble receiver."""
import hashlib
import logging
from typing import Dict, Optional

from starlette.responses import JSONResponse

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping.image_policy import playlist_collection_type
from stash_jellyfin_proxy.mapping.scene import format_jellyfin_item, is_group_favorite
from stash_jellyfin_proxy.players.matcher import resolve_from_request
from stash_jellyfin_proxy.stash.client import stash_query
from stash_jellyfin_proxy.stash.scene import get_scene_title
from stash_jellyfin_proxy.stash.tags import get_or_create_tag
from stash_jellyfin_proxy.state import streams as _streams

logger = logging.getLogger("stash-jellyfin-proxy")


_SCENE_FIELDS = (
    "id title code date details play_count resume_time last_played_at "
    "files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } "
    "studio { id name tags { name } parent_studio { id name tags { name } } } "
    "tags { name } performers { name id image_path } "
    "captions { language_code caption_type } "
    "stash_ids { stash_id }"
)


# Cache for Series-library visibility — the root /UserViews call hits this
# on every page load so we don't want a live Stash query each time. TTL is
# short because series tagging changes rarely but we want fresh-enough state.
import time as _time
_series_visibility: dict = {"expires": 0.0, "value": False}


def _series_collection_type(request) -> str:
    """CollectionType for the Series library tile.

    Swiftfin renders 'tvshows' with native Series/Season/Episode navigation.
    Infuse and SenPlayer display 'tvshows' libraries as an unnamed blank
    folder (no TV UI), so give them 'movies' — the label shows and tapping
    in renders the studios list as regular BoxSets."""
    profile = resolve_from_request(request)
    if profile.name == "swiftfin":
        return "tvshows"
    return "movies"


async def _has_playlists() -> bool:
    """True when the configured PLAYLIST_PARENT_TAG exists in Stash, regardless
    of whether it has any children yet. Showing the empty Playlists library is
    fine — it's the only place users can create their first one. Returns False
    when the feature is disabled (empty config)."""
    if not runtime.PLAYLIST_PARENT_TAG:
        return False
    tag_id = await get_or_create_tag(runtime.PLAYLIST_PARENT_TAG)
    return bool(tag_id)


async def _playlist_count() -> int:
    """Count of playlists (children of PLAYLIST_PARENT_TAG)."""
    if not runtime.PLAYLIST_PARENT_TAG:
        return 0
    parent_id = await get_or_create_tag(runtime.PLAYLIST_PARENT_TAG)
    if not parent_id:
        return 0
    try:
        res = await stash_query(
            """query PlaylistCount($pid: [ID!]) {
                findTags(tag_filter: {parents: {value: $pid, modifier: INCLUDES}}, filter: {per_page: 1}) {
                    count
                }
            }""",
            {"pid": [parent_id]},
        )
        return int(((res.get("data") or {}).get("findTags") or {}).get("count") or 0)
    except Exception as e:
        logger.debug(f"playlist count failed: {e}")
        return 0


async def _has_series_studios() -> bool:
    """True when at least one studio is tagged with SERIES_TAG. Cached
    60s to keep /UserViews cheap."""
    now = _time.monotonic()
    if now < _series_visibility["expires"]:
        return _series_visibility["value"]

    if not runtime.SERIES_TAG:
        result = False
    else:
        tag_id = await get_or_create_tag(runtime.SERIES_TAG)
        if not tag_id:
            result = False
        else:
            q = """query HasSeriesStudios($tid: [ID!]) {
                findStudios(studio_filter: {tags: {value: $tid, modifier: INCLUDES}}, filter: {per_page: 1}) {
                    count
                }
            }"""
            res = await stash_query(q, {"tid": [tag_id]})
            count = ((res.get("data") or {}).get("findStudios") or {}).get("count", 0)
            result = count > 0

    _series_visibility["value"] = result
    _series_visibility["expires"] = now + 60.0
    return result


# --- User views / virtual folders ---

def _library_image_tag(lib_id: str) -> str:
    """Per-library cache-busting tag for the library-tile image.

    Native Jellyfin clients (Infuse/SenPlayer/Swiftfin) cache images by
    (ItemId, ImageTag) and only refetch when the tag changes — they
    ignore HTTP Cache-Control for image bodies. A static string like
    "icon" means the client caches whatever it sees first forever,
    across server restarts and config changes. Mix the library id with
    a per-startup salt so every restart rotates the tag and clients
    pull fresh artwork. Within one startup the tag is stable so the
    same tile isn't refetched on every Home-screen poll."""
    salt = int(runtime.PROXY_START_TIME or 0)
    return hashlib.md5(f"{lib_id}:{salt}".encode()).hexdigest()[:16]


def _make_library(name: str, lib_id: str, collection_type: str = "movies",
                  child_count: int = 0) -> dict:
    image_tag = _library_image_tag(lib_id)
    return {
        "Name": name,
        "Id": lib_id,
        "ServerId": runtime.SERVER_ID,
        "Etag": image_tag,
        "DateCreated": "2024-01-01T00:00:00.0000000Z",
        "CanDelete": False,
        "CanDownload": False,
        "SortName": name,
        "ExternalUrls": [],
        "Path": f"/{lib_id}",
        "EnableMediaSourceDisplay": False,
        "Taglines": [],
        "Genres": [],
        "PlayAccess": "Full",
        "RemoteTrailers": [],
        "ProviderIds": {},
        "Type": "CollectionFolder",
        "CollectionType": collection_type,
        "IsFolder": True,
        "PrimaryImageAspectRatio": 1.0,
        "DisplayPreferencesId": hashlib.md5(lib_id.encode()).hexdigest()[:32],
        "Tags": [],
        "ImageTags": {"Primary": image_tag},
        "BackdropImageTags": [],
        "ScreenshotImageTags": [],
        "ImageBlurHashes": {},
        "LocationType": "FileSystem",
        "LockedFields": [],
        "LockData": False,
        "ChildCount": child_count,
        "SpecialFeatureCount": 0,
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": lib_id,
            "UnplayedItemCount": child_count,
        },
    }


async def _library_counts() -> dict:
    """Fetch real counts for the library root cards. Runs the per-library
    GraphQL count queries in parallel so /UserViews stays snappy (clients
    call this every home-screen open). Any failure falls back to 0 so a
    single missing endpoint doesn't break the root list."""
    import asyncio

    async def _count(q: str, variables: Optional[dict], path: list) -> int:
        try:
            res = await stash_query(q, variables)
            node = (res or {}).get("data") or {}
            for key in path:
                if node is None:
                    return 0
                if isinstance(key, int):
                    node = node[key] if isinstance(node, list) and len(node) > key else None
                else:
                    node = node.get(key) if isinstance(node, dict) else None
            return int(node or 0)
        except Exception as e:
            logger.debug(f"library count query failed ({path}): {e}")
            return 0

    coroutines = {
        "root-scenes": _count("query { findScenes { count } }", None, ["findScenes", "count"]),
        "root-studios": _count(
            """query { findStudios(studio_filter: {scene_count: {value: 0, modifier: GREATER_THAN}}) { count } }""",
            None, ["findStudios", "count"]),
        "root-performers": _count("query { findPerformers { count } }", None, ["findPerformers", "count"]),
        "root-groups": _count("query { findGroups { count } }", None, ["findGroups", "count"]),
    }
    if runtime.ENABLE_TAG_FILTERS:
        coroutines["root-tags"] = _count("query { findTags { count } }", None, ["findTags", "count"])
    for tag_name in runtime.TAG_GROUPS:
        tag_id = f"tag-{tag_name.lower().replace(' ', '-')}"
        coroutines[tag_id] = _count(
            """query TagSceneCount($name: String!) {
                findTags(tag_filter: {name: {value: $name, modifier: EQUALS}}, filter: {per_page: 1}) {
                    tags { scene_count }
                }
            }""",
            {"name": tag_name},
            ["findTags", "tags", 0, "scene_count"],
        )
    # series count handled separately — visibility check already fetches it.

    keys = list(coroutines.keys())
    results = await asyncio.gather(*(coroutines[k] for k in keys), return_exceptions=True)
    out = {}
    for k, v in zip(keys, results):
        out[k] = 0 if isinstance(v, Exception) else int(v or 0)
    return out


async def endpoint_user_views(request):
    """`GET /Users/{user_id}/Views` — the root library list shown as the
    sidebar/top-level entries in every client."""
    counts = await _library_counts()
    items = [
        _make_library("Scenes",     "root-scenes",     "movies", counts.get("root-scenes", 0)),
        _make_library("Studios",    "root-studios",    "movies", counts.get("root-studios", 0)),
        _make_library("Performers", "root-performers", "movies", counts.get("root-performers", 0)),
        _make_library("Groups",     "root-groups",     "movies", counts.get("root-groups", 0)),
    ]
    # Series library: appears only when at least one studio has SERIES_TAG.
    # Swiftfin gets tvshows for native Series nav; Infuse/SenPlayer get movies
    # (their tvshows renderer shows a blank/unnamed folder).
    if await _has_series_studios():
        series_count = await _series_count()
        items.append(_make_library("Series", "root-series", _series_collection_type(request), series_count))
    if await _has_playlists():
        items.append(_make_library("Playlists", "root-playlists", playlist_collection_type(request), await _playlist_count()))
    if runtime.ENABLE_TAG_FILTERS:
        items.append(_make_library("Tags", "root-tags", "movies", counts.get("root-tags", 0)))
    for tag_name in sorted(runtime.TAG_GROUPS, key=str.lower):
        tag_id = f"tag-{tag_name.lower().replace(' ', '-')}"
        items.append(_make_library(tag_name, tag_id, "movies", counts.get(tag_id, 0)))
    return JSONResponse({"Items": items, "TotalRecordCount": len(items)})


async def _series_count() -> int:
    """Count of SERIES-tagged studios for the Series library badge."""
    if not runtime.SERIES_TAG:
        return 0
    tag_id = await get_or_create_tag(runtime.SERIES_TAG)
    if not tag_id:
        return 0
    try:
        res = await stash_query(
            """query SeriesCount($tid: [ID!]) {
                findStudios(studio_filter: {tags: {value: $tid, modifier: INCLUDES}}, filter: {per_page: 1}) {
                    count
                }
            }""",
            {"tid": [tag_id]},
        )
        return int(((res.get("data") or {}).get("findStudios") or {}).get("count") or 0)
    except Exception as e:
        logger.debug(f"series count failed: {e}")
        return 0


async def endpoint_virtual_folders(request):
    """`GET /Library/VirtualFolders` — same tree as /Views but in
    Jellyfin's admin-facing shape. Infuse uses this."""
    # Locations is a non-empty list — Infuse skips libraries that report no
    # paths, leaving its local catalog empty (search, All Movies, playlists
    # all return nothing). The path is a label only; nothing reads it.
    folders = [
        {"Name": "Scenes",     "Locations": ["/stash/scenes"],     "CollectionType": "movies", "ItemId": "root-scenes"},
        {"Name": "Studios",    "Locations": ["/stash/studios"],    "CollectionType": "movies", "ItemId": "root-studios"},
        {"Name": "Performers", "Locations": ["/stash/performers"], "CollectionType": "movies", "ItemId": "root-performers"},
        {"Name": "Groups",     "Locations": ["/stash/groups"],     "CollectionType": "movies", "ItemId": "root-groups"},
    ]
    if await _has_series_studios():
        folders.append({"Name": "Series", "Locations": ["/stash/series"], "CollectionType": _series_collection_type(request), "ItemId": "root-series"})
    if await _has_playlists():
        folders.append({"Name": "Playlists", "Locations": ["/stash/playlists"], "CollectionType": playlist_collection_type(request), "ItemId": "root-playlists"})
    if runtime.ENABLE_TAG_FILTERS:
        folders.append({"Name": "Tags", "Locations": ["/stash/tags"], "CollectionType": "movies", "ItemId": "root-tags"})
    for tag_name in sorted(runtime.TAG_GROUPS, key=str.lower):
        tag_id = f"tag-{tag_name.lower().replace(' ', '-')}"
        folders.append({"Name": tag_name, "Locations": [f"/stash/{tag_id}"], "CollectionType": "movies", "ItemId": tag_id})
    return JSONResponse(folders)


# --- Home-tab rails ---

async def _warm_genre_snapshot() -> None:
    """Prefetch the genre allow-list so sync format_jellyfin_item calls
    in this request see a current snapshot."""
    from stash_jellyfin_proxy.mapping.genre import genre_allowed_names
    await genre_allowed_names()


_NEXTUP_CACHE = {"expires": 0.0, "payload": None}
_NEXTUP_TTL_SECONDS = 60.0


async def endpoint_shows_nextup(request):
    """`GET /Shows/NextUp` — Swiftfin's Home Next Up row.

    Phase 4 §8.1 algorithm:
      1. Find every SERIES-tagged studio that has at least one scene
         with play_count > 0.
      2. Within each such studio, find the most-recently-played scene.
      3. Parse its S/E; return the NEXT scene in order (same season
         next episode; end-of-season → next season episode 1).
      4. Skip series with no watched scenes or all watched.
      5. Sort output by last_played_at desc so the series you most
         recently touched shows first.

    Cached 60s per-process since this endpoint is hit on every Home-tab
    load."""
    import time as _time
    await _warm_genre_snapshot()
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 20)

    now = _time.monotonic()
    if _NEXTUP_CACHE["payload"] is not None and _NEXTUP_CACHE["expires"] > now:
        cached = _NEXTUP_CACHE["payload"]
        return JSONResponse({"Items": cached[:limit], "TotalRecordCount": min(len(cached), limit)})

    try:
        items = await _compute_nextup(limit)
    except Exception as e:
        logger.warning(f"NextUp compute failed: {e}")
        items = []

    _NEXTUP_CACHE["payload"] = items
    _NEXTUP_CACHE["expires"] = now + _NEXTUP_TTL_SECONDS
    return JSONResponse({"Items": items, "TotalRecordCount": len(items)})


async def _compute_nextup(limit: int) -> list:
    """Find each SERIES studio's next episode after its last-played scene."""
    from stash_jellyfin_proxy.stash.tags import get_or_create_tag
    from stash_jellyfin_proxy.util.series import parse_episode

    if not runtime.SERIES_TAG:
        return []
    series_tag_id = await get_or_create_tag(runtime.SERIES_TAG)
    if not series_tag_id:
        return []

    studios_q = """query SeriesStudios($tid: [ID!]) {
        findStudios(studio_filter: {tags: {value: $tid, modifier: INCLUDES}}, filter: {per_page: -1}) {
            studios { id name }
        }
    }"""
    sres = await stash_query(studios_q, {"tid": [series_tag_id]})
    studios = ((sres or {}).get("data") or {}).get("findStudios", {}).get("studios", []) or []

    candidates: list = []  # (last_played_dt_str, next_scene_dict, studio_name)

    for studio in studios:
        studio_id = studio.get("id")
        if not studio_id:
            continue
        # Pull every scene in this studio with relevant play state.
        scenes_q = f"""query SeriesScenes($sid: [ID!]) {{
            findScenes(
                scene_filter: {{studios: {{value: $sid, modifier: INCLUDES}}}},
                filter: {{per_page: -1}}
            ) {{ scenes {{ {_SCENE_FIELDS} play_count last_played_at }} }}
        }}"""
        sc_res = await stash_query(scenes_q, {"sid": [studio_id]})
        scenes = ((sc_res or {}).get("data") or {}).get("findScenes", {}).get("scenes", []) or []
        if not scenes:
            continue

        # Sort scenes into (season, episode, created_at) order so we can
        # find the logical successor.
        def _key(s):
            parsed = parse_episode(s.get("title") or "")
            se = parsed if parsed else (0, 0)
            return (se[0], se[1], s.get("created_at") or "", s.get("id"))

        scenes_ordered = sorted(scenes, key=_key)
        # Latest played scene in this studio.
        played = [s for s in scenes if (s.get("play_count") or 0) > 0 and s.get("last_played_at")]
        if not played:
            continue
        played.sort(key=lambda s: s.get("last_played_at") or "", reverse=True)
        last = played[0]

        # Find the index of `last` in the ordered list, return the NEXT
        # scene that hasn't been fully watched.
        try:
            idx = next(i for i, s in enumerate(scenes_ordered) if s.get("id") == last.get("id"))
        except StopIteration:
            continue
        next_scene = None
        for s in scenes_ordered[idx + 1:]:
            # Skip scenes that are themselves fully watched (play_count>0 and no resume).
            if (s.get("play_count") or 0) == 0:
                next_scene = s
                break
        if next_scene is None:
            # All remaining scenes already played — this series is caught
            # up. Skip it so Next Up doesn't recycle completed series.
            continue

        candidates.append((last.get("last_played_at") or "", next_scene, studio.get("name")))

    # Most recently touched series first.
    candidates.sort(key=lambda c: c[0], reverse=True)
    items = [format_jellyfin_item(c[1]) for c in candidates[:limit]]
    logger.debug(f"NextUp computed {len(items)} SERIES continuations "
                 f"across {len(candidates)} candidate series")
    return items


async def endpoint_shows_seasons(request):
    """`GET /Shows/{seriesId}/Seasons` — list Seasons for a Series.

    Seasons are synthetic: we group this studio's scenes by parsed
    ParentIndexNumber (from title regex patterns). Scenes whose titles
    don't parse land in Season 0 ("Specials")."""
    series_id = request.path_params.get("series_id", "")
    if not series_id.startswith("series-"):
        return JSONResponse({"Items": [], "TotalRecordCount": 0})
    studio_id = series_id.replace("series-", "")

    q = """query FindSeriesForSeasons($one: ID!, $sid: [ID!]) {
        findStudio(id: $one) { id name image_path }
        findScenes(
            scene_filter: {studios: {value: $sid, modifier: INCLUDES}},
            filter: {per_page: -1, sort: "date", direction: ASC}
        ) { scenes { id title } }
    }"""
    res = await stash_query(q, {"one": studio_id, "sid": [studio_id]})
    studio = res.get("data", {}).get("findStudio") or {}
    series_name = studio.get("name") or f"Series {studio_id}"
    series_image = studio.get("image_path")
    scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])

    from stash_jellyfin_proxy.util.series import parse_episode
    seasons_seen: Dict[int, int] = {}
    for scene in scenes:
        parsed = parse_episode(scene.get("title") or "")
        season_num = parsed[0] if parsed else 0
        seasons_seen[season_num] = seasons_seen.get(season_num, 0) + 1

    items = []
    for season_num in sorted(seasons_seen.keys()):
        season_id = f"season-{studio_id}-{season_num}"
        season_label = f"Season {season_num}" if season_num else "Specials"
        items.append({
            "Name": season_label,
            "SortName": f"{season_num:04d}",
            "Id": season_id,
            "ServerId": runtime.SERVER_ID,
            "Type": "Season",
            "IsFolder": True,
            "ParentId": series_id,
            "SeriesId": series_id,
            "SeriesName": series_name,
            "IndexNumber": season_num,
            "ChildCount": seasons_seen[season_num],
            "RecursiveItemCount": seasons_seen[season_num],
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}, "Backdrop": {"img": "000000"}},
            "BackdropImageTags": ["img"],
            "UserData": {
                "PlaybackPositionTicks": 0, "PlayCount": 0,
                "IsFavorite": False, "Played": False,
                "Key": season_id,
            },
        })
    return JSONResponse({"Items": items, "TotalRecordCount": len(items)})


# Only the fields format_jellyfin_item actually reads. Keep short — this
# endpoint can fetch hundreds of scenes at once for a big series.
_EPISODE_FIELDS = (
    "id title code date details play_count resume_time last_played_at "
    "files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } "
    "studio { id name tags { name } parent_studio { id name tags { name } } } "
    "tags { name } performers { name id image_path } "
    "captions { language_code caption_type } "
    "stash_ids { stash_id }"
)


async def endpoint_shows_episodes(request):
    """`GET /Shows/{id}/Episodes` — episodes for a Series, optionally
    filtered by seasonId. Swiftfin uses this two ways:
      - /Shows/series-{id}/Episodes?seasonId=season-{id}-{n}
      - /Shows/season-{id}-{n}/Episodes  (season id in the path, no query)
    Accept either."""
    await _warm_genre_snapshot()
    path_id = request.path_params.get("series_id", "")
    series_id = ""
    studio_id = ""
    want_season: Optional[int] = None

    if path_id.startswith("series-"):
        series_id = path_id
        studio_id = path_id.replace("series-", "")
    elif path_id.startswith("season-"):
        rest = path_id.replace("season-", "", 1)
        try:
            studio_id, season_str = rest.rsplit("-", 1)
            want_season = int(season_str)
            series_id = f"series-{studio_id}"
        except (ValueError, IndexError):
            return JSONResponse({"Items": [], "TotalRecordCount": 0})
    else:
        return JSONResponse({"Items": [], "TotalRecordCount": 0})

    season_id = request.query_params.get("seasonId") or request.query_params.get("SeasonId")
    if season_id and season_id.startswith("season-") and want_season is None:
        try:
            _studio, _snum = season_id.replace("season-", "", 1).rsplit("-", 1)
            if _studio == studio_id:
                want_season = int(_snum)
        except (ValueError, IndexError):
            pass
    elif want_season is not None and not season_id:
        season_id = path_id

    q = f"""query FindSeriesEpisodes($sid: [ID!]) {{
        findScenes(
            scene_filter: {{studios: {{value: $sid, modifier: INCLUDES}}}},
            filter: {{per_page: -1, sort: "date", direction: ASC}}
        ) {{ scenes {{ {_EPISODE_FIELDS} }} }}
    }}"""
    res = await stash_query(q, {"sid": [studio_id]})
    scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])

    from stash_jellyfin_proxy.util.series import parse_episode
    items = []
    for scene in scenes:
        if want_season is not None:
            parsed = parse_episode(scene.get("title") or "")
            s_num = parsed[0] if parsed else 0
            if s_num != want_season:
                continue
        parent_id_for_item = season_id if want_season is not None else series_id
        items.append(format_jellyfin_item(scene, parent_id=parent_id_for_item))
    return JSONResponse({"Items": items, "TotalRecordCount": len(items)})


async def endpoint_latest_items(request):
    """`GET /Users/{user_id}/Items/Latest` — Recently Added row per
    library. Respects LATEST_GROUPS (if configured, only listed names appear)."""
    await _warm_genre_snapshot()
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 16)
    logger.debug(f"Latest items request - ParentId: {parent_id}, Limit: {limit}")

    items = []

    def is_in_latest_groups(parent_id):
        if not runtime.LATEST_GROUPS:
            return True
        if parent_id == "root-scenes":
            return "Scenes" in runtime.LATEST_GROUPS
        if parent_id and parent_id.startswith("tag-"):
            tag_slug = parent_id[4:]
            for t in runtime.TAG_GROUPS:
                if t.lower().replace(' ', '-') == tag_slug:
                    return t in runtime.LATEST_GROUPS
        return not runtime.LATEST_GROUPS

    if not is_in_latest_groups(parent_id):
        logger.debug(f"Skipping latest for {parent_id} (not in LATEST_GROUPS)")
        return JSONResponse(items)

    if parent_id == "root-scenes":
        q = f"""query FindScenes($page: Int!, $per_page: Int!) {{
            findScenes(filter: {{page: $page, per_page: $per_page, sort: "created_at", direction: DESC}}) {{
                scenes {{ {_SCENE_FIELDS} }}
            }}
        }}"""
        res = await stash_query(q, {"page": 1, "per_page": limit})
        for s in res.get("data", {}).get("findScenes", {}).get("scenes", []):
            items.append(format_jellyfin_item(s, parent_id="root-scenes"))

    elif parent_id and parent_id.startswith("tag-"):
        tag_slug = parent_id[4:]
        tag_name = None
        for t in runtime.TAG_GROUPS:
            if t.lower().replace(' ', '-') == tag_slug:
                tag_name = t
                break
        if tag_name:
            tag_query = """query FindTags($filter: FindFilterType!) {
                findTags(filter: $filter) { tags { id name } }
            }"""
            tag_res = await stash_query(tag_query, {"filter": {"q": tag_name}})
            tags = tag_res.get("data", {}).get("findTags", {}).get("tags", [])
            tag_id = None
            for t in tags:
                if t["name"].lower() == tag_name.lower():
                    tag_id = t["id"]
                    break
            if tag_id:
                q = f"""query FindScenes($tid: [ID!], $page: Int!, $per_page: Int!) {{
                    findScenes(
                        scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
                        filter: {{page: $page, per_page: $per_page, sort: "created_at", direction: DESC}}
                    ) {{
                        scenes {{ {_SCENE_FIELDS} }}
                    }}
                }}"""
                res = await stash_query(q, {"tid": [tag_id], "page": 1, "per_page": limit})
                scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                logger.debug(f"Tag '{tag_name}' latest: {len(scenes)} scenes")
                for s in scenes:
                    items.append(format_jellyfin_item(s, parent_id=parent_id))

    elif parent_id == "root-performers":
        q = """query FindPerformers($page: Int!, $per_page: Int!) {
            findPerformers(filter: {page: $page, per_page: $per_page, sort: "created_at", direction: DESC}) {
                performers { id name image_path scene_count favorite }
            }
        }"""
        res = await stash_query(q, {"page": 1, "per_page": limit})
        for p in res.get("data", {}).get("findPerformers", {}).get("performers", []):
            item = {
                "Name": p["name"],
                "Id": f"performer-{p['id']}",
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": p.get("scene_count", 0),
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {
                    "PlaybackPositionTicks": 0, "PlayCount": 0,
                    "IsFavorite": bool(p.get("favorite")), "Played": False,
                    "Key": f"performer-{p['id']}",
                },
            }
            item["ImageTags"] = {"Primary": "img"} if p.get("image_path") else {}
            item["ImageBlurHashes"] = {"Primary": {"img": "000000"}} if p.get("image_path") else {}
            items.append(item)

    elif parent_id == "root-studios":
        q = """query FindStudios($page: Int!, $per_page: Int!) {
            findStudios(
                studio_filter: {scene_count: {value: 0, modifier: GREATER_THAN}},
                filter: {page: $page, per_page: $per_page, sort: "created_at", direction: DESC}
            ) {
                studios { id name image_path scene_count }
            }
        }"""
        res = await stash_query(q, {"page": 1, "per_page": limit})
        for s in res.get("data", {}).get("findStudios", {}).get("studios", []):
            item = {
                "Name": s["name"],
                "Id": f"studio-{s['id']}",
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": s.get("scene_count", 0),
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {
                    "PlaybackPositionTicks": 0, "PlayCount": 0,
                    "IsFavorite": False, "Played": False,
                    "Key": f"studio-{s['id']}",
                },
            }
            item["ImageTags"] = {"Primary": "img"} if s.get("image_path") else {}
            item["ImageBlurHashes"] = {"Primary": {"img": "000000"}} if s.get("image_path") else {}
            items.append(item)

    elif parent_id == "root-groups":
        q = """query FindMovies($page: Int!, $per_page: Int!) {
            findMovies(filter: {page: $page, per_page: $per_page, sort: "created_at", direction: DESC}) {
                movies { id name scene_count tags { name } }
            }
        }"""
        res = await stash_query(q, {"page": 1, "per_page": limit})
        for m in res.get("data", {}).get("findMovies", {}).get("movies", []):
            item = {
                "Name": m["name"],
                "Id": f"group-{m['id']}",
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": m.get("scene_count", 0),
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {
                    "PlaybackPositionTicks": 0, "PlayCount": 0,
                    "IsFavorite": is_group_favorite(m), "Played": False,
                    "Key": f"group-{m['id']}",
                },
            }
            items.append(item)

    # root-tags has no meaningful "latest"

    logger.debug(f"Returning {len(items)} latest items for {parent_id}")
    return JSONResponse(items)


# --- Resume list ---

async def endpoint_user_items_resume(request):
    """`GET /Users/{user_id}/Items/Resume` — scenes with a non-trivial
    resume position. Stash keeps resume_time set even after completion,
    so filter out ≥90%-watched scenes so the Continue Watching row
    doesn't fill with already-finished videos."""
    await _warm_genre_snapshot()
    RESUME_COMPLETE_THRESHOLD = 0.90
    try:
        limit = int(request.query_params.get("Limit", "24"))
    except (TypeError, ValueError):
        limit = 24
    limit = max(1, min(limit, 100))
    fetch = min(limit * 3, 100)

    q = f"""query FindScenes {{
        findScenes(
            scene_filter: {{resume_time: {{value: 0, modifier: GREATER_THAN}}}},
            filter: {{per_page: {fetch}, sort: "last_played_at", direction: DESC}}
        ) {{
            count
            scenes {{ {_SCENE_FIELDS} }}
        }}
    }}"""
    try:
        res = await stash_query(q)
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])

        def is_in_progress(scene):
            resume = scene.get("resume_time") or 0
            if resume <= 0:
                return False
            files = scene.get("files") or []
            duration = files[0].get("duration") if files else None
            if not duration or duration <= 0:
                return True  # unknown duration → keep
            return resume < duration * RESUME_COMPLETE_THRESHOLD

        in_progress = [s for s in scenes if is_in_progress(s)][:limit]
        items = [format_jellyfin_item(s) for s in in_progress]
        return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})
    except Exception as e:
        logger.error(f"Error fetching resume items: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


# --- Scrobble receiver ---

async def endpoint_sessions(request):
    """`POST /Sessions/Playing[/Progress|/Stopped]` — scrobble receiver.
    Writes resume_time to Stash on Progress; on Stopped, either auto-marks
    played (>90%) or saves final resume position, then clears the stream
    from _active_streams."""
    path = request.url.path

    try:
        body = await request.json()
    except Exception:
        body = {}

    item_id = body.get("ItemId", "")
    position_ticks = body.get("PositionTicks", 0)
    position_seconds = position_ticks / 10000000.0 if position_ticks else 0

    if "/Progress" in path and item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
        try:
            q = """mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }"""
            await stash_query(q, {"id": numeric_id, "resume_time": position_seconds})
        except Exception as e:
            logger.debug(f"Error saving resume position for {item_id}: {e}")

    elif "/Stopped" in path:
        if item_id.startswith("scene-"):
            numeric_id = item_id.replace("scene-", "")
            try:
                duration_ticks = body.get("RunTimeTicks") or body.get("NowPlayingItem", {}).get("RunTimeTicks", 0)
                duration_seconds = duration_ticks / 10000000.0 if duration_ticks else 0

                if duration_seconds <= 0:
                    try:
                        dq = """query FindScene($id: ID!) { findScene(id: $id) { files { duration } } }"""
                        dres = await stash_query(dq, {"id": numeric_id})
                        dfiles = dres.get("data", {}).get("findScene", {}).get("files", [])
                        duration_seconds = float(dfiles[0].get("duration") or 0) if dfiles else 0
                        logger.debug(f"Looked up duration from Stash for {item_id}: {duration_seconds:.0f}s")
                    except Exception:
                        pass

                played_percentage = (position_seconds / duration_seconds * 100) if duration_seconds > 0 else 0

                if played_percentage > 90:
                    await stash_query("""mutation SceneAddPlay($id: ID!) { sceneAddPlay(id: $id) { count } }""", {"id": numeric_id})
                    await stash_query(
                        """mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }""",
                        {"id": numeric_id, "resume_time": 0},
                    )
                    logger.info(f"▶ Auto-marked played: {item_id} ({played_percentage:.0f}% watched)")
                else:
                    await stash_query(
                        """mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }""",
                        {"id": numeric_id, "resume_time": position_seconds},
                    )
                    logger.info(f"⏸ Saved resume position: {item_id} at {position_seconds:.0f}s ({played_percentage:.0f}%)")
            except Exception as e:
                logger.error(f"Error updating play status for {item_id}: {e}")

        if item_id in _streams._active_streams:
            title = _streams._active_streams[item_id]["title"]
            _streams.mark_stream_stopped(item_id, from_stop_notification=True)
            logger.info(f"⏹ Stream stopped: {title} ({item_id})")
        elif item_id.startswith("scene-"):
            title = await get_scene_title(item_id)
            _streams.mark_stream_stopped(item_id, from_stop_notification=True)
            logger.info(f"⏹ Stream stopped: {title} ({item_id})")
        else:
            logger.info(f"⏹ Stream stopped: {item_id}")

    return JSONResponse({})
