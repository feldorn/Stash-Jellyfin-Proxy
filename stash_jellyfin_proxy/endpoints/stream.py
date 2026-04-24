"""Video stream, download, and subtitle proxy endpoints.

`endpoint_stream` is the hot path — it forwards Range requests byte-for-byte
from Stash to the client via StreamingResponse. Stash's 416 Requested Range
Not Satisfiable bubbles up as a 500 here today; that's a known cosmetic
issue that should be passed through as a real 416 when we do a cleanup pass.

`endpoint_download` is the same transfer but with a Content-Disposition
attachment header pointing at the original filename.

`endpoint_subtitle` pulls a VTT/SRT track from Stash and maps Jellyfin's
MediaStreams index (video + optional audio offset) back to a Stash
caption index.
"""
import logging
import os

import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.stash.client import fetch_from_stash, _get_async_client, stash_query
from stash_jellyfin_proxy.util.ids import get_numeric_id

logger = logging.getLogger("stash-jellyfin-proxy")


async def endpoint_stream(request):
    """`GET /Videos/{item_id}/stream` — proxy the scene byte stream from
    Stash, forwarding any Range header so the client can seek."""
    item_id = request.path_params.get("item_id")
    numeric_id = get_numeric_id(item_id)
    stash_stream_url = f"{runtime.STASH_URL}/scene/{numeric_id}/stream"

    logger.debug(f"Proxying stream for {item_id} from {stash_stream_url}")

    extra_headers = {}
    if "range" in request.headers:
        extra_headers["Range"] = request.headers["range"]

    try:
        client = _get_async_client()
        req = client.build_request("GET", stash_stream_url, headers=extra_headers)
        response = await client.send(req, stream=True, follow_redirects=True)
        content_type = response.headers.get("content-type", "video/mp4")

        if "text/html" in content_type:
            await response.aclose()
            logger.error(f"Got HTML response instead of video from {stash_stream_url}")
            return JSONResponse({"error": "Authentication failed"}, status_code=401)

        response.raise_for_status()

        # Status 206 only when Stash actually returned a Content-Range —
        # clients rely on this to advance seek position.
        headers = {"Accept-Ranges": "bytes"}
        status_code = 206 if "content-range" in response.headers else 200
        if status_code == 206:
            if "content-length" in response.headers:
                headers["Content-Length"] = response.headers["content-length"]
            if "content-range" in response.headers:
                headers["Content-Range"] = response.headers["content-range"]

        content_length = response.headers.get("content-length", "?")
        logger.debug(f"Stream response: {content_length} bytes, type={content_type}, status={status_code}")

        async def stream_generator():
            try:
                async for chunk in response.aiter_bytes(chunk_size=262144):
                    if chunk:
                        yield chunk
            except Exception:
                pass
            finally:
                await response.aclose()

        return StreamingResponse(
            stream_generator(),
            media_type=content_type,
            headers=headers,
            status_code=status_code,
        )

    except httpx.TimeoutException:
        logger.error(f"Stream timeout connecting to Stash: {stash_stream_url}")
        return JSONResponse({"error": "Stash timeout"}, status_code=504)
    except Exception as e:
        logger.error(f"Stream proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def endpoint_download(request):
    """`GET /Items/{item_id}/Download` — stream the full file with a
    Content-Disposition attachment pointing at the original filename."""
    item_id = request.path_params.get("item_id")
    numeric_id = get_numeric_id(item_id)
    stash_stream_url = f"{runtime.STASH_URL}/scene/{numeric_id}/stream"

    logger.info(f"Download requested for {item_id}")

    try:
        res = await stash_query(
            """query FindScene($id: ID!) { findScene(id: $id) { title files { path } } }""",
            {"id": numeric_id},
        )
        scene = res.get("data", {}).get("findScene", {})
        title = scene.get("title") or ""
        files = scene.get("files") or []
        if files:
            original_filename = os.path.basename(files[0].get("path", ""))
        else:
            original_filename = f"{title or item_id}.mp4"

        client = _get_async_client()
        req = client.build_request("GET", stash_stream_url)
        response = await client.send(req, stream=True, follow_redirects=True)
        content_type = response.headers.get("content-type", "video/mp4")

        if "text/html" in content_type:
            await response.aclose()
            logger.error(f"Got HTML response instead of video for download {stash_stream_url}")
            return JSONResponse({"error": "Authentication failed"}, status_code=401)

        response.raise_for_status()

        headers = {}
        if "content-length" in response.headers:
            headers["Content-Length"] = response.headers["content-length"]
        headers["Content-Disposition"] = f'attachment; filename="{original_filename}"'

        async def stream_generator():
            try:
                async for chunk in response.aiter_bytes(chunk_size=262144):
                    if chunk:
                        yield chunk
            except Exception:
                pass
            finally:
                await response.aclose()

        return StreamingResponse(
            stream_generator(),
            media_type=content_type,
            headers=headers,
            status_code=200,
        )

    except httpx.TimeoutException:
        logger.error(f"Download timeout connecting to Stash: {stash_stream_url}")
        return JSONResponse({"error": "Stash timeout"}, status_code=504)
    except Exception as e:
        logger.error(f"Download proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def endpoint_subtitle(request):
    """`GET /Videos/{item_id}/{source}/Subtitles/{subtitle_index}/Stream.{ext}` —
    fetch the caption file from Stash. The incoming index is
    Jellyfin-numbered (MediaStreams order: video, optional audio, then
    subtitles), so we subtract the a/v stream offset before indexing into
    Stash's captions list, with a 1-based fallback."""
    item_id = request.path_params.get("item_id")
    subtitle_index = int(request.path_params.get("subtitle_index", 1))
    numeric_id = get_numeric_id(item_id)

    try:
        result = await stash_query(
            """query FindScene($id: ID!) {
                findScene(id: $id) {
                    files { audio_codec }
                    captions { language_code caption_type }
                }
            }""",
            {"id": numeric_id},
        )
        scene_data = result.get("data", {}).get("findScene") if result else None
        if not scene_data:
            logger.error(f"Could not find scene {numeric_id} for subtitles")
            return JSONResponse({"error": "Scene not found"}, status_code=404)

        captions = scene_data.get("captions") or []
        if not captions:
            logger.warning(f"No captions found for scene {numeric_id}")
            return JSONResponse({"error": "No subtitles"}, status_code=404)

        files = scene_data.get("files", [])
        has_audio = bool(files and (files[0].get("audio_codec") or ""))
        stream_offset = 2 if has_audio else 1

        caption_idx = subtitle_index - stream_offset
        if caption_idx < 0 or caption_idx >= len(captions):
            caption_idx = subtitle_index - 1
        if caption_idx < 0 or caption_idx >= len(captions):
            logger.warning(f"Subtitle index {subtitle_index} out of range for scene {numeric_id}")
            return JSONResponse({"error": "Subtitle not found"}, status_code=404)

        caption = captions[caption_idx]
        caption_type = (caption.get("caption_type", "") or "").lower()
        if caption_type not in ("srt", "vtt"):
            caption_type = "vtt"

        lang_code = caption.get("language_code", "en") or "en"
        stash_caption_url = f"{runtime.STASH_URL}/scene/{numeric_id}/caption?lang={lang_code}&type={caption_type}"

        logger.debug(f"Proxying subtitle for {item_id} index {subtitle_index} from {stash_caption_url}")

        image_headers = {"ApiKey": runtime.STASH_API_KEY} if runtime.STASH_API_KEY else {}
        data, content_type, _ = await fetch_from_stash(stash_caption_url, extra_headers=image_headers, timeout=30)

        if caption_type == "srt":
            content_type = "application/x-subrip"
        elif caption_type == "vtt":
            content_type = "text/vtt"
        else:
            content_type = "text/plain"

        logger.debug(f"Subtitle response: {len(data)} bytes, type={content_type}")
        return Response(
            content=data,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="subtitle.{caption_type}"'},
        )

    except Exception as e:
        logger.error(f"Subtitle proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
