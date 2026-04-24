"""Search + taxonomic-list endpoints backed by Stash GraphQL queries.

Includes the counts/filters reporting + genres/persons/studios browse
+ the unified /Search/Hints endpoint Swiftfin uses.
"""
import logging

from starlette.responses import JSONResponse

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.stash.client import stash_query
from stash_jellyfin_proxy.stash.query_helpers import get_stash_sort_params, scene_filter_clause_for_parent

logger = logging.getLogger("stash-jellyfin-proxy")


async def endpoint_items_counts(request):
    """`GET /Items/Counts` — aggregate counts by Jellyfin item type."""
    try:
        count_q = """query {
            findScenes { count }
            findPerformers { count }
            findStudios { count }
            findMovies { count }
        }"""
        res = await stash_query(count_q)
        data = res.get("data", {})
        return JSONResponse({
            "MovieCount": data.get("findScenes", {}).get("count", 0),
            "SeriesCount": 0,
            "EpisodeCount": 0,
            "ArtistCount": data.get("findPerformers", {}).get("count", 0),
            "ProgramCount": 0,
            "TrailerCount": 0,
            "SongCount": 0,
            "AlbumCount": 0,
            "MusicVideoCount": 0,
            "BoxSetCount": data.get("findMovies", {}).get("count", 0),
            "BookCount": 0,
            "ItemCount": data.get("findScenes", {}).get("count", 0),
        })
    except Exception as e:
        logger.error(f"Error getting item counts: {e}")
        return JSONResponse({"ItemCount": 0})


async def endpoint_items_filters(request):
    """`GET /Items/Filters` — filter-panel options pulled from Stash tags
    and studios. Phase 3 will replace this with genre-mode-aware output."""
    try:
        tags_q = """query { findTags(filter: {per_page: 200, sort: "name", direction: ASC}) { tags { name } } }"""
        studios_q = """query { findStudios(filter: {per_page: 200, sort: "name", direction: ASC}) { studios { name } } }"""
        tags_res = await stash_query(tags_q)
        studios_res = await stash_query(studios_q)
        tag_names = [t["name"] for t in tags_res.get("data", {}).get("findTags", {}).get("tags", [])]
        studio_names = [s["name"] for s in studios_res.get("data", {}).get("findStudios", {}).get("studios", [])]
        return JSONResponse({
            "Genres": studio_names,
            "Tags": tag_names,
            "OfficialRatings": [],
            "Years": [],
        })
    except Exception as e:
        logger.error(f"Failed to fetch filters: {e}")
        return JSONResponse({"Genres": [], "Tags": [], "OfficialRatings": [], "Years": []})


async def endpoint_genres(request):
    """`GET /Genres` and `/MusicGenres` — Stash tags as Genre items.
    Optionally filtered to only those present in scenes under `parentId`."""
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    try:
        filter_clause, fvars = scene_filter_clause_for_parent(parent_id)

        if filter_clause:
            q = f"""query FindSceneTags($ids: [ID!]) {{
                findScenes({filter_clause}, filter: {{per_page: -1}}) {{
                    scenes {{ tags {{ id name }} }}
                }}
            }}"""
            res = await stash_query(q, fvars)
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
            seen = {}
            for s in scenes:
                for t in s.get("tags", []):
                    seen[t["id"]] = t["name"]
            items = [
                {"Name": name, "Id": f"genre-{tid}", "ServerId": runtime.SERVER_ID,
                 "Type": "Genre",
                 "ImageTags": {"Primary": "img"},
                 "ImageBlurHashes": {"Primary": {"img": "000000"}},
                 "BackdropImageTags": []}
                for tid, name in sorted(seen.items(), key=lambda x: x[1])
            ]
        else:
            q = """query { findTags(filter: {per_page: -1, sort: "name", direction: ASC}) {
                tags { id name scene_count }
            }}"""
            res = await stash_query(q)
            tags = res.get("data", {}).get("findTags", {}).get("tags", [])
            items = [
                {"Name": t["name"], "Id": f"genre-{t['id']}", "ServerId": runtime.SERVER_ID,
                 "Type": "Genre",
                 "ImageTags": {"Primary": "img"},
                 "ImageBlurHashes": {"Primary": {"img": "000000"}},
                 "BackdropImageTags": []}
                for t in tags if t.get("scene_count", 0) > 0
            ]
        return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})
    except Exception as e:
        logger.error(f"Error getting genres: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_persons(request):
    """`GET /Persons` — performers as Jellyfin Person items with search
    and IsFavorite filter support."""
    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or runtime.DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, runtime.MAX_PAGE_SIZE))

    search_term = request.query_params.get("searchTerm") or request.query_params.get("SearchTerm")
    filters_param = request.query_params.get("Filters") or request.query_params.get("filters") or ""
    filter_favorites = "isfavorite" in filters_param.lower()
    folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

    try:
        page = (start_index // limit) + 1

        if search_term:
            clean_search = search_term.strip('"\'')
            logger.debug(f"Persons search: '{clean_search}'")
            count_q = """query CountPerformers($q: String!) { findPerformers(filter: {q: $q}) { count } }"""
            count_res = await stash_query(count_q, {"q": clean_search})
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)
            q = """query FindPerformers($q: String!, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findPerformers(filter: {q: $q, page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = await stash_query(q, {"q": clean_search, "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
            logger.debug(f"Persons search '{clean_search}' returned {total_count} matches")
        elif filter_favorites:
            count_q = """query { findPerformers(performer_filter: {filter_favorites: true}) { count } }"""
            count_res = await stash_query(count_q)
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)
            q = """query FindFavPerformers($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findPerformers(
                    performer_filter: {filter_favorites: true},
                    filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                ) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = await stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
            logger.debug(f"Persons favorites returned {total_count} favorite performers")
        else:
            count_q = """query { findPerformers { count } }"""
            count_res = await stash_query(count_q)
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)
            q = """query FindPerformers($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findPerformers(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = await stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})

        performers = res.get("data", {}).get("findPerformers", {}).get("performers", [])
        items = []
        for p in performers:
            items.append({
                "Name": p["name"],
                "Id": f"performer-{p['id']}",
                "ServerId": runtime.SERVER_ID,
                "Type": "Person",
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
                "BackdropImageTags": [],
            })
        return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})
    except Exception as e:
        logger.error(f"Error getting persons: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_studios(request):
    """`GET /Studios` — studios as Jellyfin items, optionally filtered to
    only those present in scenes under `parentId`."""
    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or runtime.DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, runtime.MAX_PAGE_SIZE))
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

    try:
        filter_clause, fvars = scene_filter_clause_for_parent(parent_id)
        if filter_clause:
            q = f"""query FindSceneStudios($ids: [ID!]) {{
                findScenes({filter_clause}, filter: {{per_page: -1}}) {{
                    scenes {{ studio {{ id name image_path }} }}
                }}
            }}"""
            res = await stash_query(q, fvars)
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
            seen = {}
            for s in scenes:
                studio = s.get("studio")
                if studio:
                    seen[studio["id"]] = studio
            items = [
                {"Name": s["name"], "Id": f"studio-{s['id']}", "ServerId": runtime.SERVER_ID,
                 "Type": "Studio",
                 "ImageTags": {"Primary": "img"},
                 "ImageBlurHashes": {"Primary": {"img": "000000"}},
                 "BackdropImageTags": []}
                for s in sorted(seen.values(), key=lambda x: x["name"])
            ]
            return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})

        count_q = """query { findStudios { count } }"""
        count_res = await stash_query(count_q)
        total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)
        page = (start_index // limit) + 1
        q = """query FindStudios($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findStudios(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                studios { id name image_path scene_count }
            }
        }"""
        res = await stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
        studios = res.get("data", {}).get("findStudios", {}).get("studios", [])
        items = [
            {"Name": s["name"], "Id": f"studio-{s['id']}", "ServerId": runtime.SERVER_ID,
             "Type": "Studio",
             "ImageTags": {"Primary": "img"},
             "ImageBlurHashes": {"Primary": {"img": "000000"}},
             "BackdropImageTags": []}
            for s in studios
        ]
        return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})
    except Exception as e:
        logger.error(f"Error getting studios: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_search_hints(request):
    """`GET /Search/Hints` — Swiftfin's unified search. Returns typed
    SearchHints for scenes + performers interleaved."""
    search_term = request.query_params.get("searchTerm") or request.query_params.get("SearchTerm") or ""
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 20)
    limit = max(1, min(limit, 50))

    include_item_types_raw = [v for k, v in request.query_params.multi_items() if k.lower() == "includeitemtypes"]
    include_item_types = []
    for val in include_item_types_raw:
        include_item_types.extend([t.strip().lower() for t in val.split(",") if t.strip()])

    hints = []
    total_count = 0
    if not search_term.strip():
        return JSONResponse({"SearchHints": [], "TotalRecordCount": 0})

    clean_search = search_term.strip('"\'')
    search_scenes = not include_item_types or "movie" in include_item_types or "video" in include_item_types
    search_persons = not include_item_types or "person" in include_item_types

    try:
        if search_scenes:
            q = """query FindScenes($q: String!, $per_page: Int!) {
                findScenes(filter: {q: $q, per_page: $per_page, sort: "date", direction: DESC}) {
                    count
                    scenes { id title date files { duration } }
                }
            }"""
            res = await stash_query(q, {"q": clean_search, "per_page": limit})
            data = res.get("data", {}).get("findScenes", {})
            total_count += data.get("count", 0)
            for s in data.get("scenes", []):
                scene_id = f"scene-{s['id']}"
                duration = 0
                if s.get("files"):
                    duration = s["files"][0].get("duration") or 0
                title = s.get("title") or f"Scene {s['id']}"
                hint = {
                    "Name": title,
                    "Id": scene_id,
                    "ServerId": runtime.SERVER_ID,
                    "Type": "Movie",
                    "MediaType": "Video",
                    "RunTimeTicks": int(duration * 10000000),
                    "PrimaryImageTag": "img",
                    "ImageTag": "img",
                }
                date = s.get("date")
                if date:
                    hint["ProductionYear"] = int(date[:4])
                hints.append(hint)

        if search_persons:
            perf_limit = max(5, limit // 2)
            q = """query FindPerformers($q: String!, $per_page: Int!) {
                findPerformers(filter: {q: $q, per_page: $per_page}) {
                    count
                    performers { id name image_path }
                }
            }"""
            res = await stash_query(q, {"q": clean_search, "per_page": perf_limit})
            data = res.get("data", {}).get("findPerformers", {})
            total_count += data.get("count", 0)
            for p in data.get("performers", []):
                hint = {
                    "Name": p["name"],
                    "Id": f"performer-{p['id']}",
                    "ServerId": runtime.SERVER_ID,
                    "Type": "Person",
                    "MediaType": "",
                }
                if p.get("image_path"):
                    hint["PrimaryImageTag"] = "img"
                hints.append(hint)
    except Exception as e:
        logger.error(f"Search hints error: {e}")

    logger.debug(f"SearchHints '{clean_search}' -> {len(hints)} hints (total={total_count})")
    return JSONResponse({"SearchHints": hints, "TotalRecordCount": total_count})
