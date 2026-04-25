"""Image endpoint — proxies Stash images and generates PNG icons for
menu folders, tags, filters, and missing artwork.

The big dispatch block in `endpoint_image` uses item_id prefix conventions
(root-*, tag-*, tagitem-*, genre-*, filter-*, performer-*, studio-*,
group-*, scene-*) to pick the right source URL or icon generator. Placeholder
detection — tiny payloads, SVG, GIF — runs after the fetch because Stash will
happily return a 1.4 KB SVG-placeholder for items with no real image.

MENU_ICONS here is a static reference for the menu-icon id set only; the
actual PNGs are rendered by `stash_jellyfin_proxy.util.images.generate_menu_icon`.
"""
import logging
import time

from starlette.responses import Response

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping.image_policy import scene_poster_format
from stash_jellyfin_proxy.mapping.scene import is_series_scene
from stash_jellyfin_proxy.stash.client import fetch_from_stash, stash_query
from stash_jellyfin_proxy.util.ids import get_numeric_id
from stash_jellyfin_proxy.util.images import (
    PILLOW_AVAILABLE,
    compose_library_card,
    crop_to_portrait,
    fit_to_landscape,
    generate_filter_icon,
    generate_menu_icon,
    generate_placeholder_icon,
    generate_text_icon,
    menu_icon_label,
    pad_image_to_portrait,
)


def _request_wants_landscape(request) -> bool:
    """Infer tile aspect from the client's image-request query params.
    Clients like Swiftfin send fillWidth/fillHeight that reflect the
    target tile shape: a 206x309 fill is a 2:3 portrait Movie card; a
    500x281 fill is a 16:9 Home-rail card. When width > height the
    client is rendering a landscape tile and we should skip portrait
    cropping regardless of the profile's poster_format."""
    q = request.query_params
    for w_key, h_key in (("fillWidth", "fillHeight"), ("maxWidth", "maxHeight")):
        w = q.get(w_key) or q.get(w_key.lower())
        h = q.get(h_key) or q.get(h_key.lower())
        if w and h:
            try:
                if int(w) > int(h):
                    return True
                if int(h) > int(w):
                    return False
            except ValueError:
                pass
    return False

logger = logging.getLogger("stash-jellyfin-proxy")


MENU_ICONS = {
    "root-scenes", "root-studios", "root-performers",
    "root-groups", "root-series", "root-tag", "root-tags",
    "root-playlists",
}


# Library-card artwork cache (Phase 4 §8.3). Key → (expires_at_monotonic,
# bytes, content_type). 24h TTL per card. Picked scene rotates every day.
_LIBRARY_CARD_CACHE: dict = {}
_LIBRARY_CARD_TTL = 24 * 60 * 60  # seconds


async def _pick_random_scene(scene_filter_clause: str, vars_: dict) -> "str | None":
    """Return one scene id that matches scene_filter_clause, picked at
    random by Stash. scene_filter_clause is the inner part of a
    scene_filter: {...} block; pass empty string for unscoped."""
    try:
        if scene_filter_clause:
            q = f"""query PickScene($ids: [ID!]) {{
                findScenes(
                    scene_filter: {{{scene_filter_clause}}},
                    filter: {{page: 1, per_page: 1, sort: "random"}}
                ) {{ scenes {{ id }} }}
            }}"""
        else:
            q = """query PickScene {
                findScenes(filter: {page: 1, per_page: 1, sort: "random"}) {
                    scenes { id }
                }
            }"""
        res = await stash_query(q, vars_ if scene_filter_clause else None)
        scenes = ((res or {}).get("data") or {}).get("findScenes", {}).get("scenes") or []
        return scenes[0].get("id") if scenes else None
    except Exception as e:
        logger.debug(f"_pick_random_scene failed: {e}")
        return None


async def _fetch_scene_screenshot(scene_id: str) -> "tuple[bytes, str] | None":
    url = f"{runtime.STASH_URL}/scene/{scene_id}/screenshot"
    headers = {"ApiKey": runtime.STASH_API_KEY} if runtime.STASH_API_KEY else {}
    try:
        data, ct, _ = await fetch_from_stash(url, extra_headers=headers, timeout=30)
        if not data or len(data) < 500 or not (ct or "").startswith("image/"):
            return None
        return data, ct
    except Exception as e:
        logger.debug(f"scene screenshot fetch failed for {scene_id}: {e}")
        return None


async def _library_card_artwork(library_id: str) -> "tuple[bytes, str] | None":
    """Return (bytes, ct) for a library-card background image or None
    if the library is empty / Stash is unreachable. Cached 24h."""
    import time as _time
    now = _time.monotonic()
    hit = _LIBRARY_CARD_CACHE.get(library_id)
    if hit and hit[0] > now:
        return hit[1], hit[2]

    # Library cards all get a random scene screenshot — users don't
    # visually distinguish "studios library card" from "scenes library
    # card", so a random scene is fine for every root-* tile and keeps
    # this logic cheap (one query, no filter permutations to get wrong).
    scene_id = await _pick_random_scene("", None)
    if not scene_id:
        return None
    shot = await _fetch_scene_screenshot(scene_id)
    if shot is None:
        return None
    _LIBRARY_CARD_CACHE[library_id] = (now + _LIBRARY_CARD_TTL, shot[0], shot[1])
    logger.debug(f"Library card artwork {library_id} → scene-{scene_id} ({len(shot[0])} bytes)")
    return shot


async def _tag_card_artwork(tag_name: str) -> "tuple[bytes, str] | None":
    """Artwork for a TAG_GROUPS library card — random scene tagged with tag_name."""
    import time as _time
    now = _time.monotonic()
    key = f"tag:{tag_name.lower()}"
    hit = _LIBRARY_CARD_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1], hit[2]

    # Resolve tag id by name, then pick a random scene.
    try:
        tag_q = """query FindTagByName($n: String!) {
            findTags(tag_filter: {name: {value: $n, modifier: EQUALS}}, filter: {per_page: 5}) {
                tags { id name }
            }
        }"""
        tag_res = await stash_query(tag_q, {"n": tag_name})
        tags = ((tag_res or {}).get("data") or {}).get("findTags", {}).get("tags") or []
        target = next(
            (t for t in tags if (t.get("name") or "").lower() == tag_name.lower()),
            None,
        )
        if not target:
            return None
        tag_id = target.get("id")
    except Exception as e:
        logger.debug(f"tag lookup failed for '{tag_name}': {e}")
        return None

    scene_id = await _pick_random_scene(
        "tags: {value: $ids, modifier: INCLUDES}", {"ids": [tag_id]}
    )
    if not scene_id:
        return None
    shot = await _fetch_scene_screenshot(scene_id)
    if shot is None:
        return None
    _LIBRARY_CARD_CACHE[key] = (now + _LIBRARY_CARD_TTL, shot[0], shot[1])
    logger.debug(f"Tag card artwork '{tag_name}' → scene-{scene_id} ({len(shot[0])} bytes)")
    return shot


_ICON_CACHE_HEADERS = {"Cache-Control": "no-cache, must-revalidate", "Pragma": "no-cache"}
_IMAGE_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


async def endpoint_image(request):
    """Serve images for any proxy item id. Menu/tag/filter ids get
    generated PNG icons; scene/performer/studio/group ids proxy to Stash
    with fallback to a text icon or placeholder on any failure."""
    item_id = request.path_params.get("item_id")
    # Backdrop must always be landscape (design §7.4), regardless of per-client
    # poster_format. Skip portrait cropping for Backdrop and Thumb requests.
    # Also detect when the client is rendering a landscape tile and honour
    # that even for Primary requests (Home-row fillWidth>fillHeight).
    is_landscape_type = (
        "/Backdrop" in request.url.path
        or "/Thumb" in request.url.path
        or _request_wants_landscape(request)
    )

    if item_id in MENU_ICONS:
        # Library-card artwork (design §8.3) — a scene screenshot cropped
        # to 2:3 portrait, uniformly darkened to 50%, with the library
        # name overlaid in the same blue text style as the legacy text-
        # only icons. Scene pick is cached 24h upstream so the poster
        # doesn't flicker on every home-screen reopen.
        art = await _library_card_artwork(item_id)
        label = menu_icon_label(item_id)
        if art is not None:
            data, ct = compose_library_card(art[0], label)
            return Response(content=data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)
        img_data, content_type = generate_menu_icon(item_id)
        logger.debug(f"Serving fallback text icon for {item_id} (no scene artwork)")
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    if item_id.startswith("tag-"):
        # TAG_GROUPS card: scene screenshot from that tag as backdrop,
        # dimmed 50%, with the tag name overlaid. Same poster-style
        # treatment as root-* libraries. Falls back to a plain text
        # card if no tag-scoped scene is available.
        tag_slug = item_id[4:]
        tag_name = None
        for t in runtime.TAG_GROUPS:
            if t.lower().replace(' ', '-') == tag_slug:
                tag_name = t
                break
        display_name = tag_name if tag_name else tag_slug.replace('-', ' ').title()
        if tag_name:
            art = await _tag_card_artwork(tag_name)
            if art is not None:
                data, ct = compose_library_card(art[0], display_name)
                return Response(content=data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)
        img_data, content_type = generate_text_icon(display_name)
        logger.debug(f"Serving text icon for tag folder: {display_name}")
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    if item_id.startswith("genre-"):
        tag_id = item_id[6:]
        tag_img_url = f"{runtime.STASH_URL}/tag/{tag_id}/image"
        try:
            data, content_type, _ = await fetch_from_stash(tag_img_url, timeout=10)
            is_svg = content_type == "image/svg+xml"
            is_gif = content_type == "image/gif"
            is_tiny = data and len(data) < 500
            if data and len(data) > 100 and not is_svg and not is_gif and not is_tiny:
                logger.debug(f"Serving Stash image for genre {tag_id}")
                return Response(content=data, media_type=content_type, headers=_ICON_CACHE_HEADERS)
        except Exception:
            pass
        try:
            tag_res = await stash_query("query FindTag($id: ID!) { findTag(id: $id) { name } }", {"id": tag_id})
            tag_name = tag_res.get("data", {}).get("findTag", {}).get("name", tag_id)
        except Exception:
            tag_name = tag_id
        img_data, content_type = generate_text_icon(tag_name)
        logger.debug(f"Serving text icon for genre {tag_id}: {tag_name}")
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    if item_id.startswith("filters-"):
        img_data, content_type = generate_filter_icon("FILTERS")
        logger.debug(f"Serving text icon for filters folder: {item_id}")
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    if item_id.startswith("filter-"):
        parts = item_id.split("-", 2)
        if len(parts) == 3:
            filter_id = parts[2]
            res = await stash_query(
                """query FindSavedFilter($id: ID!) { findSavedFilter(id: $id) { name } }""",
                {"id": filter_id},
            )
            saved_filter = res.get("data", {}).get("findSavedFilter")
            filter_name = saved_filter.get("name", f"Filter {filter_id}") if saved_filter else f"Filter {filter_id}"
            img_data, content_type = generate_filter_icon(filter_name)
            logger.debug(f"Serving text icon for saved filter: {filter_name}")
            return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    if item_id == "tags-favorites":
        img_data, content_type = generate_filter_icon("Favorites")
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    if item_id == "tags-all":
        img_data, content_type = generate_filter_icon("All Tags")
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    if item_id.startswith("playlist-"):
        # Playlist card: pick a random scene from the playlist, dim it,
        # overlay the playlist name. Falls back to a text-only filter
        # icon if the playlist is empty or stash is unreachable.
        tag_id = item_id[len("playlist-"):]
        try:
            tag_res = await stash_query(
                """query FindTag($id: ID!) { findTag(id: $id) { name } }""",
                {"id": tag_id},
            )
            playlist_name = ((tag_res or {}).get("data") or {}).get("findTag", {}).get("name") or "Playlist"
        except Exception:
            playlist_name = "Playlist"
        scene_id = await _pick_random_scene(
            "tags: {value: $ids, modifier: INCLUDES}",
            {"ids": [tag_id]},
        )
        if scene_id:
            shot = await _fetch_scene_screenshot(scene_id)
            if shot is not None:
                data, ct = compose_library_card(shot[0], playlist_name)
                return Response(content=data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)
        img_data, content_type = generate_filter_icon(playlist_name)
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    if item_id.startswith("tagitem-"):
        tag_id = item_id.replace("tagitem-", "")
        res = await stash_query(
            """query FindTag($id: ID!) { findTag(id: $id) { name image_path } }""",
            {"id": tag_id},
        )
        tag = res.get("data", {}).get("findTag")
        if tag:
            tag_name = tag.get("name", f"Tag {tag_id}")
            if tag.get("image_path"):
                tag_img_url = f"{runtime.STASH_URL}/tag/{tag_id}/image"
                image_headers = {"ApiKey": runtime.STASH_API_KEY} if runtime.STASH_API_KEY else {}
                try:
                    data, content_type, _ = await fetch_from_stash(tag_img_url, extra_headers=image_headers, timeout=30)
                    is_svg = content_type == "image/svg+xml"
                    is_gif = content_type == "image/gif"
                    is_tiny = data and len(data) < 500
                    if data and len(data) > 100 and not is_svg and not is_gif and not is_tiny:
                        logger.debug(f"Serving Stash image for tag '{tag_name}': {len(data)} bytes")
                        return Response(content=data, media_type=content_type, headers=_ICON_CACHE_HEADERS)
                except Exception as e:
                    logger.debug(f"Failed to fetch tag image for '{tag_name}': {e}")
            img_data, content_type = generate_filter_icon(tag_name)
            return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)
        img_data, content_type = generate_filter_icon(f"Tag {tag_id}")
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    # The "placeholder" query flag is set when a group has no front_image —
    # avoids a round-trip to Stash just to get an SVG placeholder back.
    if request.query_params.get("tag", "") == "placeholder" and item_id.startswith("group-"):
        img_data, content_type = generate_placeholder_icon("group")
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    # Item-type → Stash URL mapping
    needs_portrait_resize = False
    is_group_image = False
    numeric_id = None

    if item_id.startswith("studio-"):
        numeric_id = item_id.replace("studio-", "")
        stash_img_url = f"{runtime.STASH_URL}/studio/{numeric_id}/image"
        needs_portrait_resize = not is_landscape_type
    elif item_id.startswith("series-"):
        # A Series id wraps a studio id — fetch the studio's image.
        numeric_id = item_id.replace("series-", "")
        stash_img_url = f"{runtime.STASH_URL}/studio/{numeric_id}/image"
        needs_portrait_resize = not is_landscape_type
    elif item_id.startswith("season-"):
        # season-<studio_id>-<season_num> — reuse the series/studio image.
        # Swiftfin renders Season cards as 16:9 landscape (same template as
        # Episodes), so never portrait-crop Season images.
        rest = item_id.replace("season-", "", 1)
        try:
            numeric_id, _ = rest.rsplit("-", 1)
        except ValueError:
            numeric_id = rest
        stash_img_url = f"{runtime.STASH_URL}/studio/{numeric_id}/image"
        needs_portrait_resize = False
    elif item_id.startswith("performer-") or item_id.startswith("person-"):
        if item_id.startswith("person-performer-"):
            numeric_id = item_id.replace("person-performer-", "")
        elif item_id.startswith("performer-"):
            numeric_id = item_id.replace("performer-", "")
        else:
            numeric_id = item_id.replace("person-", "")
        stash_img_url = f"{runtime.STASH_URL}/performer/{numeric_id}/image"
    elif item_id.startswith("group-"):
        numeric_id = item_id.replace("group-", "")
        stash_img_url = f"{runtime.STASH_URL}/group/{numeric_id}/frontimage?t={int(time.time())}"
        is_group_image = True
    elif item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
        stash_img_url = f"{runtime.STASH_URL}/scene/{numeric_id}/screenshot"
        # Scenes in SERIES-tagged studios are Episodes. Swiftfin renders
        # Episode tiles as 16:9 landscape, so force landscape regardless of
        # the profile's poster_format. Cache the is-series check per scene
        # to avoid a Stash roundtrip on every image request.
        is_episode_scene = runtime.SERIES_SCENE_CACHE.get(numeric_id)
        if is_episode_scene is None and runtime.SERIES_TAG:
            try:
                res = await stash_query(
                    """query SceneSeries($id: ID!) { findScene(id: $id) {
                        studio { tags { name } parent_studio { tags { name } } }
                    } }""",
                    {"id": numeric_id},
                )
                scene_doc = (res or {}).get("data", {}).get("findScene") or {}
                is_episode_scene = is_series_scene(scene_doc)
            except Exception as e:
                logger.debug(f"is_series_scene lookup failed for {item_id}: {e}")
                is_episode_scene = False
            runtime.SERIES_SCENE_CACHE[numeric_id] = is_episode_scene
        if is_episode_scene:
            needs_portrait_resize = False
        else:
            needs_portrait_resize = (not is_landscape_type) and scene_poster_format(request) == "portrait"
    else:
        numeric_id = get_numeric_id(item_id)
        stash_img_url = f"{runtime.STASH_URL}/scene/{numeric_id}/screenshot"

    logger.debug(f"Proxying image for {item_id} from {stash_img_url}")

    if is_landscape_type:
        format_key = "landscape"
    elif item_id.startswith("scene-"):
        format_key = "landscape" if not needs_portrait_resize else scene_poster_format(request)
    else:
        format_key = "portrait" if needs_portrait_resize else "original"
    cache_key = (item_id, format_key)
    if cache_key in runtime.IMAGE_CACHE:
        cached_data, cached_type = runtime.IMAGE_CACHE[cache_key]
        logger.debug(f"Cache hit for {item_id}")
        return Response(content=cached_data, media_type=cached_type, headers=_IMAGE_CACHE_HEADERS)

    image_headers = {"ApiKey": runtime.STASH_API_KEY} if runtime.STASH_API_KEY else {}

    async def _name_text_icon(iid: str, nid: str):
        """Query Stash for the item's display name and render a text icon as a
        fallback when the actual image is missing/invalid."""
        name = None
        try:
            if iid.startswith("performer-") or iid.startswith("person-"):
                res = await stash_query("query($id: ID!) { findPerformer(id: $id) { name } }", {"id": nid})
                name = (res.get("data", {}).get("findPerformer") or {}).get("name")
            elif iid.startswith("studio-") or iid.startswith("series-") or iid.startswith("season-"):
                res = await stash_query("query($id: ID!) { findStudio(id: $id) { name } }", {"id": nid})
                name = (res.get("data", {}).get("findStudio") or {}).get("name")
            elif iid.startswith("scene-"):
                res = await stash_query("query($id: ID!) { findScene(id: $id) { title } }", {"id": nid})
                name = (res.get("data", {}).get("findScene") or {}).get("title")
        except Exception:
            pass
        img_data, ct = generate_text_icon(name or iid)
        runtime.IMAGE_CACHE[cache_key] = (img_data, ct)
        return img_data, ct

    async def _parent_studio_logo(studio_numeric_id: str):
        """Fetch the parent studio's image (network logo) when the studio
        itself has no real image. E.g. NF Busty falls back to the Nubiles
        Porn Network logo. Returns (bytes, content_type) or None."""
        try:
            res = await stash_query(
                """query ParentStudioImage($id: ID!) {
                    findStudio(id: $id) { parent_studio { id } }
                }""",
                {"id": studio_numeric_id},
            )
            parent = ((res.get("data") or {}).get("findStudio") or {}).get("parent_studio") or {}
            parent_id = parent.get("id")
            if not parent_id:
                return None
            parent_url = f"{runtime.STASH_URL}/studio/{parent_id}/image"
            p_data, p_ct, _ = await fetch_from_stash(parent_url, extra_headers=image_headers, timeout=30)
            # Reject SVG placeholders and too-small blobs — same test the
            # caller uses for the main image.
            if (not p_data or len(p_data) < 500
                or not (p_ct or "").startswith("image/")
                or p_ct == "image/svg+xml"):
                return None
            return p_data, p_ct
        except Exception as e:
            logger.debug(f"parent-studio fallback failed for studio-{studio_numeric_id}: {e}")
            return None

    async def _studio_scene_fallback(studio_numeric_id: str):
        """When a studio/series/season has no valid Stash image, borrow a
        scene screenshot from that studio so Swiftfin's hero banners and tile
        posters aren't empty text cards. Returns (bytes, content_type) or None."""
        try:
            res = await stash_query(
                """query PickStudioScene($sid: [ID!]) {
                    findScenes(
                        scene_filter: {studios: {value: $sid, modifier: INCLUDES}},
                        filter: {page: 1, per_page: 1, sort: "random"}
                    ) { scenes { id } }
                }""",
                {"sid": [studio_numeric_id]},
            )
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", []) or []
            if not scenes:
                return None
            scene_id = scenes[0].get("id")
            if not scene_id:
                return None
            scene_url = f"{runtime.STASH_URL}/scene/{scene_id}/screenshot"
            s_data, s_ct, _ = await fetch_from_stash(scene_url, extra_headers=image_headers, timeout=30)
            if not s_data or len(s_data) < 500 or not (s_ct or "").startswith("image/"):
                return None
            return s_data, s_ct
        except Exception as e:
            logger.debug(f"studio-scene fallback failed for studio-{studio_numeric_id}: {e}")
            return None

    try:
        data, content_type, _ = await fetch_from_stash(stash_img_url, extra_headers=image_headers, timeout=30)

        if item_id.startswith(("performer-", "person-", "studio-", "scene-", "series-", "season-")):
            is_invalid = (
                not data or len(data) < 500
                or (content_type and not content_type.startswith("image/"))
                or content_type == "image/svg+xml"
            )
            if is_invalid:
                # For studios / series / seasons with no Stash image, try the
                # parent studio's logo first (e.g. NF Busty → Nubiles Porn
                # Network), then a random scene screenshot, then the text
                # icon. Gives Swiftfin a real logo whenever possible.
                if item_id.startswith(("studio-", "series-", "season-")):
                    parent_logo = await _parent_studio_logo(numeric_id)
                    if parent_logo is not None:
                        data, content_type = parent_logo
                        logger.debug(f"Used parent-studio logo fallback for {item_id}")
                    else:
                        scene_fb = await _studio_scene_fallback(numeric_id)
                        if scene_fb is not None:
                            data, content_type = scene_fb
                            # Scene screenshots are 16:9 — crop to portrait
                            # when resizing, don't letterbox.
                            source_is_scene = True
                            logger.debug(f"Used scene-screenshot fallback for {item_id}")
                        else:
                            logger.debug(f"No fallback for {item_id}, generating text icon")
                            img_data, ct = await _name_text_icon(item_id, numeric_id)
                            return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)
                else:
                    logger.debug(f"No valid image for {item_id}, generating text icon")
                    img_data, ct = await _name_text_icon(item_id, numeric_id)
                    return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)

        if not data or len(data) < 100:
            if item_id.startswith("group-"):
                logger.debug(f"Empty/small response for group, using placeholder: {item_id}")
                img_data, ct = generate_placeholder_icon("group")
                return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)

        if content_type and not content_type.startswith("image/"):
            if item_id.startswith("group-"):
                logger.debug(f"Non-image response for group ({content_type}), using placeholder: {item_id}")
                img_data, ct = generate_placeholder_icon("group")
                return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)

        # Stash returns a ~1.4KB SVG placeholder for groups with no art. If
        # we see that, try GraphQL front_image_path as a second path before
        # giving up and rendering the local placeholder.
        if is_group_image and content_type == "image/svg+xml":
            logger.warning(f"Got SVG placeholder for {item_id}, trying GraphQL fallback")
            try:
                gql_res = await stash_query(
                    """query FindGroup($id: ID!) { findGroup(id: $id) { front_image_path } }""",
                    {"id": numeric_id},
                )
                gql_data = gql_res.get("data", {}).get("findGroup") if gql_res else None
                front_image_path = gql_data.get("front_image_path") if gql_data else None
                # Stash returns front_image_path as an absolute URL with its
                # own ?t=… cache buster. If `default=true` is in there, Stash
                # already told us it has no real artwork — short-circuit to
                # the local placeholder instead of refetching another SVG.
                if front_image_path and "default=true" in front_image_path:
                    img_data, ct = generate_placeholder_icon("group")
                    return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)
                if front_image_path:
                    gql_img_url = (
                        front_image_path
                        if front_image_path.startswith(("http://", "https://"))
                        else f"{runtime.STASH_URL}{front_image_path}"
                    )
                    logger.debug(f"GraphQL fallback: fetching from {gql_img_url}")
                    data, content_type, _ = await fetch_from_stash(gql_img_url, extra_headers=image_headers, timeout=30)
                    if not (data and len(data) > 1000 and content_type != "image/svg+xml"):
                        logger.warning("GraphQL fallback still returned placeholder/SVG")
                        img_data, ct = generate_placeholder_icon("group")
                        return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)
                else:
                    logger.warning(f"No front_image_path in GraphQL response for {item_id}")
                    img_data, ct = generate_placeholder_icon("group")
                    return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)
            except Exception as e:
                logger.error(f"GraphQL fallback failed for {item_id}: {e}")
                img_data, ct = generate_placeholder_icon("group")
                return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)

        if needs_portrait_resize and runtime.ENABLE_IMAGE_RESIZE and PILLOW_AVAILABLE:
            # Scenes (and scene-screenshot fallbacks for studios) get cropped
            # to 2:3 — scenes are 16:9 landscape and center-cropping produces
            # a clean poster. Studio logos keep the letterbox-pad so text
            # wordmarks don't get chopped in half.
            if item_id.startswith("scene-") or locals().get("source_is_scene"):
                anchor = getattr(runtime, "POSTER_CROP_ANCHOR", "center") or "center"
                data, content_type = crop_to_portrait(data, 400, 600, anchor=anchor)
                logger.debug("Cropped to 400x600 portrait (2:3)")
            else:
                data, content_type = pad_image_to_portrait(data, target_width=400, target_height=600)
                logger.debug("Letterbox-padded to 400x600 portrait (2:3)")
            if len(runtime.IMAGE_CACHE) >= runtime.IMAGE_CACHE_MAX_SIZE:
                oldest_key = next(iter(runtime.IMAGE_CACHE))
                del runtime.IMAGE_CACHE[oldest_key]
            runtime.IMAGE_CACHE[cache_key] = (data, content_type)
        elif is_landscape_type and runtime.ENABLE_IMAGE_RESIZE and PILLOW_AVAILABLE:
            # Landscape target (Backdrop, Thumb, landscape tile). Route through
            # fit_to_landscape so portrait phone-video screenshots don't get
            # stretched by the client's hero renderer — we produce a blurred
            # full-frame background with the original centered on top at its
            # natural aspect. Sources that already match 16:9 short-circuit
            # inside fit_to_landscape (just a resize, no blur).
            data, content_type = fit_to_landscape(data)
            logger.debug("Fit to 1920x1080 landscape (blur-background when source aspect differs)")
            if len(runtime.IMAGE_CACHE) >= runtime.IMAGE_CACHE_MAX_SIZE:
                oldest_key = next(iter(runtime.IMAGE_CACHE))
                del runtime.IMAGE_CACHE[oldest_key]
            runtime.IMAGE_CACHE[cache_key] = (data, content_type)

        logger.debug(f"Image response: {len(data)} bytes, type={content_type}")
        return Response(content=data, media_type=content_type, headers=_IMAGE_CACHE_HEADERS)

    except Exception as e:
        logger.error(f"Image proxy error for {item_id}: {e}")
        if item_id.startswith("group-"):
            img_data, ct = generate_placeholder_icon("group")
            return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)
        if item_id.startswith(("performer-", "person-", "studio-", "scene-", "series-", "season-")):
            img_data, ct = await _name_text_icon(item_id, numeric_id)
            return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)
        from stash_jellyfin_proxy.util.images import placeholder_png
        return Response(content=placeholder_png(), media_type='image/png', headers=_IMAGE_CACHE_HEADERS)
