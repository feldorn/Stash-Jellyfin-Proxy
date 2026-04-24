"""User-action endpoints — favorites, played/unplayed, favorite list.

Favorites delegate to different Stash fields per entity type:
 - Scenes + Groups: tag toggle with FAVORITE_TAG
 - Performers + Studios: native `favorite` boolean via the update mutation

Played/unplayed use sceneAddPlay / sceneDeletePlay and resume-time reset.
"""
import logging

from starlette.responses import JSONResponse

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping.scene import format_jellyfin_item
from stash_jellyfin_proxy.stash.client import stash_query
from stash_jellyfin_proxy.stash.tags import get_or_create_tag

logger = logging.getLogger("stash-jellyfin-proxy")


# --- Favorite list ---

_SCENE_FIELDS = (
    "id title code date details play_count resume_time last_played_at "
    "files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } "
    "studio { name } tags { name } performers { name id image_path } "
    "captions { language_code caption_type }"
)


async def endpoint_user_favorites(request):
    """`GET /Users/{user_id}/FavoriteItems` — scenes tagged with FAVORITE_TAG."""
    if not runtime.FAVORITE_TAG:
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
    try:
        tag_id = await get_or_create_tag(runtime.FAVORITE_TAG)
        if not tag_id:
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
        q = f"""query FindScenes($tag_ids: [ID!]) {{
            findScenes(scene_filter: {{tags: {{value: $tag_ids, modifier: INCLUDES}}}}, filter: {{per_page: 100, sort: "updated_at", direction: DESC}}) {{
                count
                scenes {{ {_SCENE_FIELDS} }}
            }}
        }}"""
        res = await stash_query(q, {"tag_ids": [tag_id]})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        count = res.get("data", {}).get("findScenes", {}).get("count", 0)
        items = [format_jellyfin_item(s) for s in scenes]
        return JSONResponse({"Items": items, "TotalRecordCount": count, "StartIndex": 0})
    except Exception as e:
        logger.error(f"Error fetching favorites: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


# --- Favorite toggle helpers ---

def _extract_performer_id(item_id: str) -> str:
    """Handle the three performer-id shapes Jellyfin clients may send:
    performer-<n>, person-<n>, or person-performer-<n> (Swiftfin)."""
    if item_id.startswith("person-performer-"):
        return item_id.replace("person-performer-", "")
    if item_id.startswith("performer-"):
        return item_id.replace("performer-", "")
    return item_id.replace("person-", "")


async def _toggle_scene_favorite(item_id: str, add: bool) -> None:
    numeric_id = item_id.replace("scene-", "")
    tag_id = await get_or_create_tag(runtime.FAVORITE_TAG)
    if not tag_id:
        return
    scene_res = await stash_query(
        """query FindScene($id: ID!) { findScene(id: $id) { id tags { id } } }""",
        {"id": numeric_id},
    )
    scene = scene_res.get("data", {}).get("findScene") if scene_res else None
    if not scene:
        return
    existing = [t["id"] for t in scene.get("tags", []) if t["id"] != tag_id]
    if add:
        existing.append(tag_id)
    await stash_query(
        """mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }""",
        {"input": {"id": numeric_id, "tag_ids": existing}},
    )
    logger.info(f"{'★ Favorited' if add else '☆ Unfavorited'} scene: {item_id}")


async def _toggle_group_favorite(item_id: str, add: bool) -> None:
    group_id = item_id.replace("group-", "")
    tag_id = await get_or_create_tag(runtime.FAVORITE_TAG)
    if not tag_id:
        return
    group_res = await stash_query(
        """query FindMovie($id: ID!) { findMovie(id: $id) { id tags { id } } }""",
        {"id": group_id},
    )
    group = group_res.get("data", {}).get("findMovie") if group_res else None
    if not group:
        return
    existing = [t["id"] for t in group.get("tags", []) if t["id"] != tag_id]
    if add:
        existing.append(tag_id)
    await stash_query(
        """mutation MovieUpdate($input: MovieUpdateInput!) { movieUpdate(input: $input) { id } }""",
        {"input": {"id": group_id, "tag_ids": existing}},
    )
    logger.info(f"{'★ Favorited' if add else '☆ Unfavorited'} group: {item_id}")


async def _toggle_performer_favorite(item_id: str, add: bool) -> None:
    pid = _extract_performer_id(item_id)
    await stash_query(
        """mutation PerformerUpdate($input: PerformerUpdateInput!) { performerUpdate(input: $input) { id favorite } }""",
        {"input": {"id": pid, "favorite": add}},
    )
    logger.info(f"{'★ Favorited' if add else '☆ Unfavorited'} performer: {item_id}")


async def _toggle_studio_favorite(item_id: str, add: bool) -> None:
    sid = item_id.replace("studio-", "")
    await stash_query(
        """mutation StudioUpdate($input: StudioUpdateInput!) { studioUpdate(input: $input) { id favorite } }""",
        {"input": {"id": sid, "favorite": add}},
    )
    logger.info(f"{'★ Favorited' if add else '☆ Unfavorited'} studio: {item_id}")


def _favorite_response(item_id: str, is_favorite: bool) -> JSONResponse:
    return JSONResponse({
        "IsFavorite": is_favorite,
        "PlaybackPositionTicks": 0,
        "PlayCount": 0,
        "Played": False,
        "Key": item_id,
        "ItemId": item_id,
    })


async def endpoint_user_item_favorite(request):
    """Mark item as favorite. Scene/Group: tag toggle.  Performer/Studio: native flag."""
    item_id = request.path_params.get("item_id", "")
    try:
        if item_id.startswith("scene-"):
            if not runtime.FAVORITE_TAG:
                return _favorite_response(item_id, True)
            await _toggle_scene_favorite(item_id, add=True)
        elif item_id.startswith("group-"):
            if not runtime.FAVORITE_TAG:
                return _favorite_response(item_id, True)
            await _toggle_group_favorite(item_id, add=True)
        elif item_id.startswith("performer-") or item_id.startswith("person-"):
            await _toggle_performer_favorite(item_id, add=True)
        elif item_id.startswith("studio-"):
            await _toggle_studio_favorite(item_id, add=True)
    except Exception as e:
        logger.error(f"Error favoriting {item_id}: {e}")
    return _favorite_response(item_id, True)


async def endpoint_user_item_unfavorite(request):
    """Remove favorite. Scene/Group: untag.  Performer/Studio: native flag."""
    item_id = request.path_params.get("item_id", "")
    try:
        if item_id.startswith("scene-"):
            if not runtime.FAVORITE_TAG:
                return _favorite_response(item_id, False)
            await _toggle_scene_favorite(item_id, add=False)
        elif item_id.startswith("group-"):
            if not runtime.FAVORITE_TAG:
                return _favorite_response(item_id, False)
            await _toggle_group_favorite(item_id, add=False)
        elif item_id.startswith("performer-") or item_id.startswith("person-"):
            await _toggle_performer_favorite(item_id, add=False)
        elif item_id.startswith("studio-"):
            await _toggle_studio_favorite(item_id, add=False)
    except Exception as e:
        logger.error(f"Error unfavoriting {item_id}: {e}")
    return _favorite_response(item_id, False)


# --- Played / unplayed ---

async def endpoint_user_played_items(request):
    """Mark a scene as played (increments sceneAddPlay in Stash)."""
    item_id = request.path_params.get("item_id", "")
    if item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
        try:
            q = """mutation SceneAddPlay($id: ID!) { sceneAddPlay(id: $id) { count } }"""
            result = await stash_query(q, {"id": numeric_id})
            new_count = (result.get("data", {}).get("sceneAddPlay") or {}).get("count") if result else None
            if new_count is not None:
                logger.info(f"▶ Marked played: {item_id} (play count: {new_count})")
            else:
                logger.warning(f"Failed to mark played {item_id}: {result}")
        except Exception as e:
            logger.error(f"Error marking played {item_id}: {e}")
    return JSONResponse({"PlayCount": 1, "Played": True, "IsFavorite": False, "PlaybackPositionTicks": 0})


async def endpoint_user_unplayed_items(request):
    """Mark a scene as unplayed — clears history and resets resume time.
    sceneDeletePlay removes one play at a time, so loop until empty."""
    item_id = request.path_params.get("item_id", "")
    if item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
        try:
            dq = """mutation SceneDeletePlay($id: ID!) { sceneDeletePlay(id: $id) { count } }"""
            for _ in range(1000):
                res = await stash_query(dq, {"id": numeric_id})
                count = (res.get("data", {}).get("sceneDeletePlay") or {}).get("count") if res else 0
                if not count:
                    break
            aq = """mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }"""
            await stash_query(aq, {"id": numeric_id, "resume_time": 0})
            logger.info(f"⏮ Marked unplayed: {item_id}")
        except Exception as e:
            logger.error(f"Error marking unplayed {item_id}: {e}")
    return JSONResponse({"PlayCount": 0, "Played": False, "IsFavorite": False, "PlaybackPositionTicks": 0})
