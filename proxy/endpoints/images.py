"""Image endpoint — proxies Stash images and generates PNG icons for
menu folders, tags, filters, and missing artwork.

The big dispatch block in `endpoint_image` uses item_id prefix conventions
(root-*, tag-*, tagitem-*, genre-*, filter-*, performer-*, studio-*,
group-*, scene-*) to pick the right source URL or icon generator. Placeholder
detection — tiny payloads, SVG, GIF — runs after the fetch because Stash will
happily return a 1.4 KB SVG-placeholder for items with no real image.

MENU_ICONS here is a static reference for the menu-icon id set only; the
actual PNGs are rendered by `proxy.util.images.generate_menu_icon`.
"""
import logging
import time

from starlette.responses import Response

from proxy import runtime
from proxy.stash.client import fetch_from_stash, stash_query
from proxy.util.ids import get_numeric_id
from proxy.util.images import (
    PILLOW_AVAILABLE,
    generate_filter_icon,
    generate_menu_icon,
    generate_placeholder_icon,
    generate_text_icon,
    pad_image_to_portrait,
)

logger = logging.getLogger("stash-jellyfin-proxy")


MENU_ICONS = {
    "root-scenes", "root-studios", "root-performers",
    "root-groups", "root-tag", "root-tags",
}


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

    if item_id in MENU_ICONS:
        img_data, content_type = generate_menu_icon(item_id)
        logger.debug(f"Serving menu icon for {item_id}")
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    if item_id.startswith("tag-"):
        tag_slug = item_id[4:]
        tag_name = None
        for t in runtime.TAG_GROUPS:
            if t.lower().replace(' ', '-') == tag_slug:
                tag_name = t
                break
        display_name = tag_name if tag_name else tag_slug.replace('-', ' ').title()
        img_data, content_type = generate_text_icon(display_name)
        logger.debug(f"Serving text icon for tag folder: {display_name}")
        return Response(content=img_data, media_type=content_type, headers=_ICON_CACHE_HEADERS)

    if item_id.startswith("genre-"):
        tag_id = item_id[6:]
        tag_img_url = f"{runtime.STASH_URL}/tag/{tag_id}/image"
        try:
            data, content_type, _ = fetch_from_stash(tag_img_url, timeout=10)
            is_svg = content_type == "image/svg+xml"
            is_gif = content_type == "image/gif"
            is_tiny = data and len(data) < 500
            if data and len(data) > 100 and not is_svg and not is_gif and not is_tiny:
                logger.debug(f"Serving Stash image for genre {tag_id}")
                return Response(content=data, media_type=content_type, headers=_ICON_CACHE_HEADERS)
        except Exception:
            pass
        try:
            tag_res = stash_query("query FindTag($id: ID!) { findTag(id: $id) { name } }", {"id": tag_id})
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
            res = stash_query(
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

    if item_id.startswith("tagitem-"):
        tag_id = item_id.replace("tagitem-", "")
        res = stash_query(
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
                    data, content_type, _ = fetch_from_stash(tag_img_url, extra_headers=image_headers, timeout=30)
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
        needs_portrait_resize = True
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
    else:
        numeric_id = get_numeric_id(item_id)
        stash_img_url = f"{runtime.STASH_URL}/scene/{numeric_id}/screenshot"

    logger.debug(f"Proxying image for {item_id} from {stash_img_url}")

    cache_key = (item_id, "portrait" if needs_portrait_resize else "original")
    if cache_key in runtime.IMAGE_CACHE:
        cached_data, cached_type = runtime.IMAGE_CACHE[cache_key]
        logger.debug(f"Cache hit for {item_id}")
        return Response(content=cached_data, media_type=cached_type, headers=_IMAGE_CACHE_HEADERS)

    image_headers = {"ApiKey": runtime.STASH_API_KEY} if runtime.STASH_API_KEY else {}

    def _name_text_icon(iid: str, nid: str):
        """Query Stash for the item's display name and render a text icon as a
        fallback when the actual image is missing/invalid."""
        name = None
        try:
            if iid.startswith("performer-") or iid.startswith("person-"):
                res = stash_query("query($id: ID!) { findPerformer(id: $id) { name } }", {"id": nid})
                name = (res.get("data", {}).get("findPerformer") or {}).get("name")
            elif iid.startswith("studio-"):
                res = stash_query("query($id: ID!) { findStudio(id: $id) { name } }", {"id": nid})
                name = (res.get("data", {}).get("findStudio") or {}).get("name")
            elif iid.startswith("scene-"):
                res = stash_query("query($id: ID!) { findScene(id: $id) { title } }", {"id": nid})
                name = (res.get("data", {}).get("findScene") or {}).get("title")
        except Exception:
            pass
        img_data, ct = generate_text_icon(name or iid)
        runtime.IMAGE_CACHE[cache_key] = (img_data, ct)
        return img_data, ct

    try:
        data, content_type, _ = fetch_from_stash(stash_img_url, extra_headers=image_headers, timeout=30)

        if item_id.startswith(("performer-", "person-", "studio-", "scene-")):
            is_invalid = (
                not data or len(data) < 500
                or (content_type and not content_type.startswith("image/"))
                or content_type == "image/svg+xml"
            )
            if is_invalid:
                logger.debug(f"No valid image for {item_id}, generating text icon")
                img_data, ct = _name_text_icon(item_id, numeric_id)
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
                gql_res = stash_query(
                    """query FindGroup($id: ID!) { findGroup(id: $id) { front_image_path } }""",
                    {"id": numeric_id},
                )
                gql_data = gql_res.get("data", {}).get("findGroup") if gql_res else None
                front_image_path = gql_data.get("front_image_path") if gql_data else None
                if front_image_path:
                    gql_img_url = f"{runtime.STASH_URL}{front_image_path}?t={int(time.time())}"
                    logger.debug(f"GraphQL fallback: fetching from {gql_img_url}")
                    data, content_type, _ = fetch_from_stash(gql_img_url, extra_headers=image_headers, timeout=30)
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
            data, content_type = pad_image_to_portrait(data, target_width=400, target_height=600)
            logger.debug("Resized studio image to 400x600 portrait (2:3)")
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
        if item_id.startswith(("performer-", "person-", "studio-", "scene-")):
            img_data, ct = _name_text_icon(item_id, numeric_id)
            return Response(content=img_data, media_type=ct, headers=_IMAGE_CACHE_HEADERS)
        from proxy.util.images import placeholder_png
        return Response(content=placeholder_png(), media_type='image/png', headers=_IMAGE_CACHE_HEADERS)
