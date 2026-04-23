"""Home-tab and library-browse endpoints — Views, VirtualFolders,
Next Up, Latest, Resume, and the Sessions scrobble receiver."""
import hashlib
import logging

from starlette.responses import JSONResponse

from proxy import runtime
from proxy.mapping.scene import format_jellyfin_item, is_group_favorite
from proxy.stash.client import stash_query
from proxy.stash.scene import get_scene_title
from proxy.state import streams as _streams

logger = logging.getLogger("stash-jellyfin-proxy")


_SCENE_FIELDS = (
    "id title code date details play_count resume_time last_played_at "
    "files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } "
    "studio { name } tags { name } performers { name id image_path } "
    "captions { language_code caption_type }"
)


# --- User views / virtual folders ---

def _make_library(name: str, lib_id: str) -> dict:
    return {
        "Name": name,
        "Id": lib_id,
        "ServerId": runtime.SERVER_ID,
        "Etag": hashlib.md5(lib_id.encode()).hexdigest()[:16],
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
        "CollectionType": "movies",
        "IsFolder": True,
        "PrimaryImageAspectRatio": 1.0,
        "DisplayPreferencesId": hashlib.md5(lib_id.encode()).hexdigest()[:32],
        "Tags": [],
        "ImageTags": {"Primary": "icon"},
        "BackdropImageTags": [],
        "ScreenshotImageTags": [],
        "ImageBlurHashes": {},
        "LocationType": "FileSystem",
        "LockedFields": [],
        "LockData": False,
        "ChildCount": 100,
        "SpecialFeatureCount": 0,
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": lib_id,
            "UnplayedItemCount": 100,
        },
    }


async def endpoint_user_views(request):
    """`GET /Users/{user_id}/Views` — the root library list shown as the
    sidebar/top-level entries in every client."""
    items = [
        _make_library("Scenes", "root-scenes"),
        _make_library("Studios", "root-studios"),
        _make_library("Performers", "root-performers"),
        _make_library("Groups", "root-groups"),
    ]
    if runtime.ENABLE_TAG_FILTERS:
        items.append(_make_library("Tags", "root-tags"))
    for tag_name in sorted(runtime.TAG_GROUPS, key=str.lower):
        tag_id = f"tag-{tag_name.lower().replace(' ', '-')}"
        items.append(_make_library(tag_name, tag_id))
    return JSONResponse({"Items": items, "TotalRecordCount": len(items)})


async def endpoint_virtual_folders(request):
    """`GET /Library/VirtualFolders` — same tree as /Views but in
    Jellyfin's admin-facing shape. Infuse uses this."""
    folders = [
        {"Name": "Scenes", "Locations": [], "CollectionType": "movies", "ItemId": "root-scenes"},
        {"Name": "Studios", "Locations": [], "CollectionType": "movies", "ItemId": "root-studios"},
        {"Name": "Performers", "Locations": [], "CollectionType": "movies", "ItemId": "root-performers"},
        {"Name": "Groups", "Locations": [], "CollectionType": "movies", "ItemId": "root-groups"},
    ]
    if runtime.ENABLE_TAG_FILTERS:
        folders.append({"Name": "Tags", "Locations": [], "CollectionType": "movies", "ItemId": "root-tags"})
    for tag_name in sorted(runtime.TAG_GROUPS, key=str.lower):
        tag_id = f"tag-{tag_name.lower().replace(' ', '-')}"
        folders.append({"Name": tag_name, "Locations": [], "CollectionType": "movies", "ItemId": tag_id})
    return JSONResponse(folders)


# --- Home-tab rails ---

async def endpoint_shows_nextup(request):
    """`GET /Shows/NextUp` — Swiftfin's home-page Next Up row. Stash has
    no notion of episode succession, so return random suggestions. Phase
    4 will replace this with a proper SERIES-aware algorithm."""
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 20)
    q = f"""query FindScenes($page: Int!, $per_page: Int!) {{
        findScenes(filter: {{page: $page, per_page: $per_page, sort: "random", direction: DESC}}) {{
            findScenes: scenes {{ {_SCENE_FIELDS} }}
        }}
    }}"""
    try:
        res = stash_query(q, {"page": 1, "per_page": limit})
        scenes = res.get("data", {}).get("findScenes", {}).get("findScenes", [])
        items = [format_jellyfin_item(s) for s in scenes]
        logger.debug(f"NextUp returning {len(items)} random suggestions")
        return JSONResponse({"Items": items, "TotalRecordCount": len(items)})
    except Exception as e:
        logger.warning(f"NextUp query failed: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0})


async def endpoint_latest_items(request):
    """`GET /Users/{user_id}/Items/Latest` — Recently Added row per
    library. Respects LATEST_GROUPS (if configured, only listed names appear)."""
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
        res = stash_query(q, {"page": 1, "per_page": limit})
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
            tag_res = stash_query(tag_query, {"filter": {"q": tag_name}})
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
                res = stash_query(q, {"tid": [tag_id], "page": 1, "per_page": limit})
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
        res = stash_query(q, {"page": 1, "per_page": limit})
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
            findStudios(filter: {page: $page, per_page: $per_page, sort: "created_at", direction: DESC}) {
                studios { id name image_path scene_count }
            }
        }"""
        res = stash_query(q, {"page": 1, "per_page": limit})
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
        res = stash_query(q, {"page": 1, "per_page": limit})
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
        res = stash_query(q)
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
            stash_query(q, {"id": numeric_id, "resume_time": position_seconds})
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
                        dres = stash_query(dq, {"id": numeric_id})
                        dfiles = dres.get("data", {}).get("findScene", {}).get("files", [])
                        duration_seconds = float(dfiles[0].get("duration") or 0) if dfiles else 0
                        logger.debug(f"Looked up duration from Stash for {item_id}: {duration_seconds:.0f}s")
                    except Exception:
                        pass

                played_percentage = (position_seconds / duration_seconds * 100) if duration_seconds > 0 else 0

                if played_percentage > 90:
                    stash_query("""mutation SceneAddPlay($id: ID!) { sceneAddPlay(id: $id) { count } }""", {"id": numeric_id})
                    stash_query(
                        """mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }""",
                        {"id": numeric_id, "resume_time": 0},
                    )
                    logger.info(f"▶ Auto-marked played: {item_id} ({played_percentage:.0f}% watched)")
                else:
                    stash_query(
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
            title = get_scene_title(item_id)
            _streams.mark_stream_stopped(item_id, from_stop_notification=True)
            logger.info(f"⏹ Stream stopped: {title} ({item_id})")
        else:
            logger.info(f"⏹ Stream stopped: {item_id}")

    return JSONResponse({})
