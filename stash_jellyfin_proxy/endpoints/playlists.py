"""Playlist endpoints — Jellyfin Playlists backed by Stash tags.

A configured parent tag (`runtime.PLAYLIST_PARENT_TAG`) is the namespace.
Each child of that parent tag is one playlist. The scenes carrying a
child tag are the playlist's items.

Full Jellyfin PlaylistsController surface:

  - POST   /Playlists                              → create
  - GET    /Playlists/{id}                         → metadata (OpenAccess, ItemIds)
  - POST   /Playlists/{id}                         → rename (Name in JSON body)
  - DELETE /Items/{id}  (when id startswith playlist-) → delete the playlist tag
  - GET    /Playlists/{id}/Items                   → list scenes
  - POST   /Playlists/{id}/Items                   → attach
  - DELETE /Playlists/{id}/Items                   → detach
  - POST   /Playlists/{id}/Items/{itemId}/Move/{n} → reorder (persisted in state/playlist_order)
  - GET    /Playlists/{id}/Users                   → single-user stub
  - GET    /Playlists/{id}/Users/{userId}          → single-user stub
  - POST   /Playlists/{id}/Users/{userId}          → accept (single-user proxy)
  - DELETE /Playlists/{id}/Users/{userId}          → accept (single-user proxy)

The tag-must-be-a-child-of-PLAYLIST_PARENT_TAG check guards every mutation
so we never modify a tag that wasn't created as a playlist.
"""
import json as _json
import logging

from starlette.responses import JSONResponse, Response

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping.genre import invalidate_playlist_tag_names
from stash_jellyfin_proxy.mapping.image_policy import playlist_item_type
from stash_jellyfin_proxy.mapping.scene import format_jellyfin_item
from stash_jellyfin_proxy.stash.client import stash_query
from stash_jellyfin_proxy.stash.tags import get_or_create_tag
from stash_jellyfin_proxy.state import playlist_order

logger = logging.getLogger("stash-jellyfin-proxy")


# Same scene field bag used elsewhere — keeps format_jellyfin_item happy.
_SCENE_FIELDS = (
    "id title code date details play_count resume_time last_played_at "
    "files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } "
    "studio { id name tags { name } parent_studio { id name tags { name } } } "
    "tags { name } performers { name id image_path } "
    "captions { language_code caption_type } "
    "stash_ids { stash_id }"
)


def _strip_playlist_prefix(raw: str) -> str:
    """Accept 'playlist-NNN', 'NNN', or a padded GUID and return numeric tag id."""
    if not raw:
        return ""
    if raw.startswith("playlist-"):
        return raw[len("playlist-"):]
    if "-" in raw:
        # Padded GUID form — collapse and strip leading zeros.
        digits = raw.replace("-", "").lstrip("0")
        return digits or "0"
    return raw


def _split_csv_ids(raw: str) -> list:
    """Split Jellyfin's ids/entryIds csv params, stripping scene- prefix
    and any GUID padding. Returns numeric Stash scene IDs."""
    if not raw:
        return []
    out = []
    for piece in raw.split(","):
        s = piece.strip()
        if not s:
            continue
        if s.startswith("scene-"):
            s = s[len("scene-"):]
        elif "-" in s:
            s = s.replace("-", "").lstrip("0") or "0"
        out.append(s)
    return out


async def _ensure_parent_tag_id() -> str:
    """Return the configured playlist-parent tag id, creating it if needed."""
    if not runtime.PLAYLIST_PARENT_TAG:
        return ""
    return await get_or_create_tag(runtime.PLAYLIST_PARENT_TAG) or ""


async def _is_playlist_tag(tag_id: str) -> bool:
    """True iff the tag's parent is the configured playlist parent.
    Guards every mutation: callers can't modify arbitrary tags by id."""
    parent_id = await _ensure_parent_tag_id()
    if not parent_id or not tag_id:
        return False
    res = await stash_query(
        """query TagParents($id: ID!) { findTag(id: $id) { id parents { id } } }""",
        {"id": tag_id},
    )
    tag = (res or {}).get("data", {}).get("findTag")
    if not tag:
        return False
    return any(p.get("id") == parent_id for p in (tag.get("parents") or []))


async def _attach_tag(scene_id: str, tag_id: str) -> None:
    """Add tag_id to scene_id's tag list, preserving existing tags."""
    scene_res = await stash_query(
        """query FindScene($id: ID!) { findScene(id: $id) { id tags { id } } }""",
        {"id": scene_id},
    )
    scene = (scene_res or {}).get("data", {}).get("findScene")
    if not scene:
        return
    existing = [t["id"] for t in scene.get("tags", []) if t["id"] != tag_id]
    existing.append(tag_id)
    await stash_query(
        """mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }""",
        {"input": {"id": scene_id, "tag_ids": existing}},
    )


async def _detach_tag(scene_id: str, tag_id: str) -> None:
    """Remove tag_id from scene_id's tag list."""
    scene_res = await stash_query(
        """query FindScene($id: ID!) { findScene(id: $id) { id tags { id } } }""",
        {"id": scene_id},
    )
    scene = (scene_res or {}).get("data", {}).get("findScene")
    if not scene:
        return
    existing = [t["id"] for t in scene.get("tags", []) if t["id"] != tag_id]
    await stash_query(
        """mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }""",
        {"input": {"id": scene_id, "tag_ids": existing}},
    )


async def _create_playlist_tag(name: str, parent_id: str) -> str:
    """Create a child tag under parent_id with the given name, return its id."""
    res = await stash_query(
        """mutation TagCreate($input: TagCreateInput!) { tagCreate(input: $input) { id name } }""",
        {"input": {"name": name, "parent_ids": [parent_id]}},
    )
    tag = (res or {}).get("data", {}).get("tagCreate")
    return tag.get("id") if tag else ""


async def ordered_playlist_scenes(tag_id: str, fields: str = "id") -> list:
    """Every scene carrying the playlist tag, in persisted playlist order.

    Playlists are small, so we pull the whole membership in one query and
    order/page in Python — Stash can't sort by an external ordering."""
    q = f"""query PlaylistScenes($tid: [ID!]) {{
        findScenes(
            scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
            filter: {{per_page: -1, sort: "id", direction: ASC}}
        ) {{ scenes {{ {fields} }} }}
    }}"""
    res = await stash_query(q, {"tid": [tag_id]})
    scenes = (((res or {}).get("data") or {}).get("findScenes") or {}).get("scenes") or []
    by_id = {str(s["id"]): s for s in scenes if s.get("id") is not None}
    return [by_id[sid] for sid in playlist_order.apply_order(tag_id, by_id.keys())]


async def endpoint_create_playlist(request):
    """`POST /Playlists?name=X&ids=...&userId=...` — create a new playlist
    (child tag of PLAYLIST_PARENT_TAG) and attach the given scenes to it."""
    if not runtime.PLAYLIST_PARENT_TAG:
        logger.warning("POST /Playlists: feature disabled (PLAYLIST_PARENT_TAG empty)")
        return JSONResponse({"error": "Playlists disabled"}, status_code=400)

    name = (request.query_params.get("name") or request.query_params.get("Name") or "").strip()
    ids_raw = request.query_params.get("ids") or request.query_params.get("Ids") or ""
    if not name:
        return JSONResponse({"error": "Missing name"}, status_code=400)

    parent_id = await _ensure_parent_tag_id()
    if not parent_id:
        logger.error("POST /Playlists: could not resolve/create parent tag")
        return JSONResponse({"error": "Failed to ensure parent tag"}, status_code=500)

    try:
        tag_id = await _create_playlist_tag(name, parent_id)
        if not tag_id:
            return JSONResponse({"error": "Tag creation failed"}, status_code=500)

        scene_ids = _split_csv_ids(ids_raw)
        for sid in scene_ids:
            await _attach_tag(sid, tag_id)
        playlist_order.append(tag_id, scene_ids)

        invalidate_playlist_tag_names()
        playlist_guid = f"playlist-{tag_id}"
        logger.info(f"♬ Created playlist '{name}' ({playlist_guid}) with {len(scene_ids)} scenes")
        return JSONResponse({"Id": playlist_guid})
    except Exception as e:
        logger.error(f"POST /Playlists error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def endpoint_playlist_add_items(request):
    """`POST /Playlists/{id}/Items?ids=...` — attach playlist tag to scenes."""
    pid = request.path_params.get("playlist_id", "")
    tag_id = _strip_playlist_prefix(pid)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"error": "Not a playlist"}, status_code=404)

    ids_raw = request.query_params.get("ids") or request.query_params.get("Ids") or ""
    scene_ids = _split_csv_ids(ids_raw)
    try:
        for sid in scene_ids:
            await _attach_tag(sid, tag_id)
        playlist_order.append(tag_id, scene_ids)
        logger.info(f"♬ Added {len(scene_ids)} scenes to playlist-{tag_id}")
        return JSONResponse({})
    except Exception as e:
        logger.error(f"POST /Playlists/{pid}/Items error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def endpoint_playlist_remove_items(request):
    """`DELETE /Playlists/{id}/Items?entryIds=...` — detach playlist tag.

    Jellyfin uses entryIds (not ids) here; per the spec, EntryIds are the
    *playlist-entry* GUIDs, but real clients (Infuse, web) send the
    underlying scene ids. We treat both equivalently."""
    pid = request.path_params.get("playlist_id", "")
    tag_id = _strip_playlist_prefix(pid)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"error": "Not a playlist"}, status_code=404)

    raw = (
        request.query_params.get("entryIds")
        or request.query_params.get("EntryIds")
        or request.query_params.get("ids")
        or request.query_params.get("Ids")
        or ""
    )
    scene_ids = _split_csv_ids(raw)
    try:
        for sid in scene_ids:
            await _detach_tag(sid, tag_id)
        playlist_order.remove(tag_id, scene_ids)
        logger.info(f"♬ Removed {len(scene_ids)} scenes from playlist-{tag_id}")
        return JSONResponse({})
    except Exception as e:
        logger.error(f"DELETE /Playlists/{pid}/Items error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def endpoint_playlist_items(request):
    """`GET /Playlists/{id}/Items` — list scenes carrying the playlist tag."""
    pid = request.path_params.get("playlist_id", "")
    tag_id = _strip_playlist_prefix(pid)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or runtime.DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, runtime.MAX_PAGE_SIZE))

    try:
        # Warm genre cache so format_jellyfin_item's sync genre split works.
        from stash_jellyfin_proxy.mapping.genre import genre_allowed_names
        await genre_allowed_names()

        all_scenes = await ordered_playlist_scenes(tag_id, _SCENE_FIELDS)
        scenes = all_scenes[start_index:start_index + limit]
        items = []
        for s in scenes:
            it = format_jellyfin_item(s)
            # PlaylistItemId — Jellyfin uses this to identify a specific
            # playlist row (since the same item can appear multiple times).
            # Tags have at most one attachment per scene, so the scene id
            # uniquely identifies the row. Round-trips through DELETE
            # /Playlists/{id}/Items?entryIds=<id> exactly as Infuse expects.
            it["PlaylistItemId"] = it.get("Id")
            it["ParentId"] = f"playlist-{tag_id}"
            items.append(it)
        return JSONResponse({
            "Items": items,
            "TotalRecordCount": len(all_scenes),
            "StartIndex": start_index,
        })
    except Exception as e:
        logger.error(f"GET /Playlists/{pid}/Items error: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})


async def list_playlists(request, start_index: int = 0, limit: int = 0) -> dict:
    """Return all playlists (children of PLAYLIST_PARENT_TAG) shaped as
    Jellyfin items. Used by endpoint_items when ParentId=root-playlists.

    Returns dict with keys Items, TotalRecordCount, StartIndex."""
    parent_id = await _ensure_parent_tag_id()
    if not parent_id:
        return {"Items": [], "TotalRecordCount": 0, "StartIndex": start_index}

    item_type = playlist_item_type(request)
    limit = max(1, min(limit or runtime.DEFAULT_PAGE_SIZE, runtime.MAX_PAGE_SIZE))
    try:
        q = """query Playlists($pid: [ID!]) {
            findTags(
                tag_filter: {parents: {value: $pid, modifier: INCLUDES}},
                filter: {per_page: -1, sort: "name", direction: ASC}
            ) {
                count
                tags { id name scene_count image_path created_at updated_at }
            }
        }"""
        res = await stash_query(q, {"pid": [parent_id]})
        data = (res or {}).get("data", {}).get("findTags", {}) or {}
        tags = data.get("tags") or []
        all_items = [_playlist_item(t, item_type=item_type) for t in tags]
        page = all_items[start_index:start_index + limit]
        return {
            "Items": page,
            "TotalRecordCount": data.get("count", len(all_items)),
            "StartIndex": start_index,
        }
    except Exception as e:
        logger.error(f"list_playlists error: {e}")
        return {"Items": [], "TotalRecordCount": 0, "StartIndex": start_index}


async def get_playlist_item(request, tag_id: str) -> dict:
    """Return a single playlist as a Jellyfin item dict, or {} if not a playlist."""
    if not await _is_playlist_tag(tag_id):
        return {}
    res = await stash_query(
        """query PlaylistTag($id: ID!) { findTag(id: $id) { id name scene_count image_path created_at updated_at } }""",
        {"id": tag_id},
    )
    tag = (res or {}).get("data", {}).get("findTag")
    if not tag:
        return {}
    return _playlist_item(tag, item_type=playlist_item_type(request))


async def endpoint_get_playlist(request):
    """`GET /Playlists/{id}` — playlist metadata in Jellyfin's PlaylistDto
    shape: OpenAccess, Shares, ItemIds. We're a single-user proxy so
    OpenAccess is always false and Shares is always [].

    Infuse pulls this when opening the playlist's "edit" sheet."""
    pid = request.path_params.get("playlist_id", "")
    tag_id = _strip_playlist_prefix(pid)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"error": "Not a playlist"}, status_code=404)
    try:
        scenes = await ordered_playlist_scenes(tag_id)
        return JSONResponse({
            "OpenAccess": False,
            "Shares": [],
            "ItemIds": [f"scene-{s['id']}" for s in scenes],
        })
    except Exception as e:
        logger.error(f"GET /Playlists/{pid} error: {e}")
        return JSONResponse({"OpenAccess": False, "Shares": [], "ItemIds": []})


async def endpoint_update_playlist(request):
    """`POST /Playlists/{id}` — UpdatePlaylist. Renames the underlying tag
    when a Name is provided in the JSON body. Other UpdatePlaylistDto fields
    (Ids, Users, IsPublic) are accepted-and-ignored: Ids overwrites are a
    rare client gesture, Users/IsPublic are meaningless on a single-user
    proxy."""
    pid = request.path_params.get("playlist_id", "")
    tag_id = _strip_playlist_prefix(pid)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"error": "Not a playlist"}, status_code=404)

    try:
        body = await request.body()
        payload = _json.loads(body) if body else {}
    except Exception:
        payload = {}
    new_name = (payload.get("Name") or payload.get("name") or "").strip()
    if not new_name:
        return JSONResponse({})  # nothing to do; treat as success

    try:
        await stash_query(
            """mutation TagUpdate($input: TagUpdateInput!) { tagUpdate(input: $input) { id name } }""",
            {"input": {"id": tag_id, "name": new_name}},
        )
        invalidate_playlist_tag_names()
        logger.info(f"♬ Renamed playlist-{tag_id} → '{new_name}'")
        return JSONResponse({})
    except Exception as e:
        logger.error(f"POST /Playlists/{pid} error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def endpoint_delete_playlist(request):
    """`DELETE /Items/{id}` (when id starts with playlist-) — destroy the
    underlying child tag. Non-playlist ids are passed through so we don't
    accidentally accept scene/performer/studio deletes."""
    item_id = request.path_params.get("item_id", "")
    if not item_id.startswith("playlist-"):
        return JSONResponse({"error": "Deletion not supported for this item type"}, status_code=405)
    tag_id = _strip_playlist_prefix(item_id)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"error": "Not a playlist"}, status_code=404)
    try:
        await stash_query(
            """mutation TagDestroy($input: TagDestroyInput!) { tagDestroy(input: $input) }""",
            {"input": {"id": tag_id}},
        )
        invalidate_playlist_tag_names()
        playlist_order.forget(tag_id)
        logger.info(f"♬ Deleted playlist-{tag_id}")
        return Response(status_code=204)
    except Exception as e:
        logger.error(f"DELETE /Items/{item_id} error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def endpoint_playlist_move_item(request):
    """`POST /Playlists/{id}/Items/{itemId}/Move/{newIndex}` — reorder.

    Stash tags are unordered, so the order lives in state/playlist_order
    (playlist_order.json beside the config). itemId is the PlaylistItemId,
    which for us is the scene id (a tag attaches to a scene at most once)."""
    pid = request.path_params.get("playlist_id", "")
    tag_id = _strip_playlist_prefix(pid)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"error": "Not a playlist"}, status_code=404)

    item_ids = _split_csv_ids(request.path_params.get("item_id", ""))
    try:
        new_index = int(request.path_params.get("new_index", ""))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid index"}, status_code=400)
    if len(item_ids) != 1 or new_index < 0:
        return JSONResponse({"error": "Invalid item or index"}, status_code=400)

    try:
        members = [s["id"] for s in await ordered_playlist_scenes(tag_id)]
        if not playlist_order.move(tag_id, members, item_ids[0], new_index):
            return JSONResponse({"error": "Item not in playlist"}, status_code=404)
        logger.info(f"♬ Moved scene-{item_ids[0]} to index {new_index} in playlist-{tag_id}")
        return Response(status_code=204)
    except Exception as e:
        logger.error(f"POST /Playlists/{pid}/Items/.../Move error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _stub_user_record() -> dict:
    """The only "user" the proxy recognises — derived from runtime.SJS_USER.
    PlaylistsController returns PlaylistUserPermissions records here."""
    return {
        "UserId": runtime.USER_ID,
        "UserName": runtime.SJS_USER or "user",
        "CanEdit": True,
    }


async def endpoint_playlist_users(request):
    """`GET /Playlists/{id}/Users` — list of users with access. Single-user
    proxy: always returns the configured user with CanEdit=true."""
    pid = request.path_params.get("playlist_id", "")
    tag_id = _strip_playlist_prefix(pid)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"error": "Not a playlist"}, status_code=404)
    return JSONResponse([_stub_user_record()])


async def endpoint_playlist_user(request):
    """`GET /Playlists/{id}/Users/{userId}` — single-user lookup."""
    pid = request.path_params.get("playlist_id", "")
    tag_id = _strip_playlist_prefix(pid)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"error": "Not a playlist"}, status_code=404)
    return JSONResponse(_stub_user_record())


async def endpoint_playlist_user_update(request):
    """`POST /Playlists/{id}/Users/{userId}` — accept-and-noop. Single-user
    proxy has no permission model to update."""
    pid = request.path_params.get("playlist_id", "")
    tag_id = _strip_playlist_prefix(pid)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"error": "Not a playlist"}, status_code=404)
    return Response(status_code=204)


async def endpoint_playlist_user_remove(request):
    """`DELETE /Playlists/{id}/Users/{userId}` — accept-and-noop."""
    pid = request.path_params.get("playlist_id", "")
    tag_id = _strip_playlist_prefix(pid)
    if not tag_id or not await _is_playlist_tag(tag_id):
        return JSONResponse({"error": "Not a playlist"}, status_code=404)
    return Response(status_code=204)


def _iso_jellyfin(ts) -> str:
    """Stash RFC3339 ('2026-11-17T15:41:43-06:00') → Jellyfin UTC
    ('2026-11-17T21:41:43.0000000Z'). '' if unparseable."""
    if not ts:
        return ""
    import datetime as _dt
    try:
        d = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _playlist_item(tag: dict, item_type: str = "Playlist") -> dict:
    """Shape a Stash tag row as a Jellyfin playlist item.

    item_type is "Playlist" for native renderers (Infuse/web) and "BoxSet"
    for compat clients (Swiftfin/SenPlayer) so their existing folder view
    handles navigation since they don't render the native Playlist type.

    Always emit ImageTags — the proxy renders a poster card on demand
    (see images.py "playlist-" handler), so we don't gate on whether
    Stash has an uploaded image_path for the tag itself."""
    pid = f"playlist-{tag['id']}"
    name = tag.get("name") or "Untitled"
    count = int(tag.get("scene_count") or 0)
    out = {
        "Name": name,
        "SortName": name.lower(),
        "Id": pid,
        "ServerId": runtime.SERVER_ID,
        "Type": item_type,
        "IsFolder": True,
        "ParentId": "root-playlists",
        "ChildCount": count,
        "RecursiveItemCount": count,
        "PrimaryImageAspectRatio": 0.6667,
        "ImageTags": {"Primary": "img"},
        "ImageBlurHashes": {"Primary": {"img": "000000"}},
        "BackdropImageTags": [],
        # Fields a real Jellyfin 10.10 server sends on every playlist, so
        # clients that sort/filter on them (DateCreated etc.) see the same shape.
        "CanDelete": True,
        "LocationType": "FileSystem",
        "PlayAccess": "Full",
        "UserData": {
            "PlaybackPositionTicks": 0, "PlayCount": 0,
            "IsFavorite": False, "Played": False,
            "UnplayedItemCount": count, "Key": pid,
        },
    }
    created = _iso_jellyfin(tag.get("created_at"))
    if created:
        out["DateCreated"] = created
    updated = _iso_jellyfin(tag.get("updated_at"))
    if updated:
        out["DateLastMediaAdded"] = updated
    if item_type == "Playlist":
        out["MediaType"] = "Video"  # Playlist requires MediaType for native UI
    elif item_type == "Series":
        # Series shape (playlist_as_series): no CollectionType, one season.
        out["BackdropImageTags"] = ["img"]
        out["ImageBlurHashes"]["Backdrop"] = {"img": "000000"}
    else:
        out["CollectionType"] = "movies"  # BoxSet needs this so Swiftfin enters it
    return out


# ─── Playlist-as-Series shape (issue #29, playlist_as_series profiles) ──────
#
# SenPlayer only builds a Next/Previous queue for Episodes: it opens a Series,
# loads /Shows/{seriesId}/Episodes?SeasonId=..., and steps through that list.
# For profiles with playlist_as_series, a playlist is a Series (id
# playlist-N) with exactly one Season (id plseason-N) whose Episodes are the
# playlist scenes in stored order, IndexNumber = position.

PLSEASON_PREFIX = "plseason-"


def plseason_tag_id(raw: str) -> str:
    """plseason-N → N ('' if not a numeric plseason id)."""
    if not raw or not raw.startswith(PLSEASON_PREFIX):
        return ""
    rest = raw[len(PLSEASON_PREFIX):]
    return rest if rest.isdigit() else ""


async def _playlist_name(tag_id: str) -> str:
    res = await stash_query(
        """query PlaylistName($id: ID!) { findTag(id: $id) { name scene_count } }""",
        {"id": tag_id},
    )
    tag = (res or {}).get("data", {}).get("findTag") or {}
    return tag.get("name") or "Playlist"


def _season_dto(tag_id: str, name: str, count: int) -> dict:
    season_id = f"{PLSEASON_PREFIX}{tag_id}"
    series_id = f"playlist-{tag_id}"
    return {
        "Name": name,
        "SortName": "0001",
        "Id": season_id,
        "ServerId": runtime.SERVER_ID,
        "Type": "Season",
        "IsFolder": True,
        "ParentId": series_id,
        "SeriesId": series_id,
        "SeriesName": name,
        "IndexNumber": 1,
        "ChildCount": count,
        "RecursiveItemCount": count,
        "ImageTags": {"Primary": "img"},
        "ImageBlurHashes": {"Primary": {"img": "000000"}, "Backdrop": {"img": "000000"}},
        "BackdropImageTags": ["img"],
        "UserData": {
            "PlaybackPositionTicks": 0, "PlayCount": 0,
            "IsFavorite": False, "Played": False, "Key": season_id,
        },
    }


async def playlist_series_seasons(tag_id: str) -> dict:
    """/Shows/playlist-N/Seasons → the single synthetic season."""
    if not await _is_playlist_tag(tag_id):
        return {"Items": [], "TotalRecordCount": 0}
    name = await _playlist_name(tag_id)
    count = len(await ordered_playlist_scenes(tag_id, "id"))
    return {"Items": [_season_dto(tag_id, name, count)], "TotalRecordCount": 1}


async def playlist_season_item(tag_id: str) -> dict:
    """Item detail for plseason-N ({} if not a playlist)."""
    if not await _is_playlist_tag(tag_id):
        return {}
    name = await _playlist_name(tag_id)
    count = len(await ordered_playlist_scenes(tag_id, "id"))
    return _season_dto(tag_id, name, count)


async def playlist_series_episodes(tag_id: str) -> dict:
    """/Shows/playlist-N/Episodes → playlist scenes as ordered Episodes."""
    if not await _is_playlist_tag(tag_id):
        return {"Items": [], "TotalRecordCount": 0}
    from stash_jellyfin_proxy.mapping.genre import genre_allowed_names
    await genre_allowed_names()
    name = await _playlist_name(tag_id)
    series_id = f"playlist-{tag_id}"
    season_id = f"{PLSEASON_PREFIX}{tag_id}"
    scenes = await ordered_playlist_scenes(tag_id, _SCENE_FIELDS)
    items = []
    for pos, s in enumerate(scenes, start=1):
        it = format_jellyfin_item(s, parent_id=season_id)
        it.update({
            "Type": "Episode",
            "SeriesId": series_id,
            "SeriesName": name,
            "SeasonId": season_id,
            "SeasonName": name,
            "ParentIndexNumber": 1,
            "IndexNumber": pos,
            "PlaylistItemId": it.get("Id"),
        })
        items.append(it)
    return {"Items": items, "TotalRecordCount": len(items)}
