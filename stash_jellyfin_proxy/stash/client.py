"""Stash GraphQL client + binary-fetch helper — the modules that talk to
Stash directly.

Reads connection config from stash_jellyfin_proxy.runtime. Writes STASH_VERSION /
STASH_CONNECTED / GRAPHQL_URL back to runtime as side effects.

Async by default: stash_query() and fetch_from_stash() are async coroutines
that share a single httpx.AsyncClient (connection-pooled, non-blocking).
check_stash_connection() is intentionally sync — it's called from main()
before the event loop starts.
"""
import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.cache.ttl import TTLCache

logger = logging.getLogger("stash-jellyfin-proxy")

_status_cache = TTLCache(ttl_seconds=30.0)

# Module-level async client, lazily initialised on first request.
_async_client: Optional[httpx.AsyncClient] = None


def _graphql_url() -> str:
    url = f"{runtime.STASH_URL.rstrip('/')}{runtime.STASH_GRAPHQL_PATH}"
    runtime.GRAPHQL_URL = url
    return url


def _auth_headers() -> Dict[str, str]:
    if runtime.STASH_API_KEY:
        return {"ApiKey": runtime.STASH_API_KEY}
    return {}


def _get_async_client() -> httpx.AsyncClient:
    """Return (or lazily create) the shared async HTTP client."""
    global _async_client
    if _async_client is None:
        if not runtime.STASH_VERIFY_TLS:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except ImportError:
                pass
            logger.info("TLS verification disabled (STASH_VERIFY_TLS=false)")
        if runtime.STASH_API_KEY:
            logger.info(f"Session configured with ApiKey header (key length: {len(runtime.STASH_API_KEY)})")
        else:
            logger.warning("No STASH_API_KEY configured - images will fail to load!")
            logger.warning("Add STASH_API_KEY to your config file (get from Stash -> Settings -> Security)")
        _async_client = httpx.AsyncClient(
            verify=runtime.STASH_VERIFY_TLS,
            headers=_auth_headers(),
            follow_redirects=True,
        )
    return _async_client


def check_stash_connection() -> bool:
    """Sync connection check for startup (before the event loop). Uses httpx.Client."""
    try:
        url = _graphql_url()
        logger.info(f"Testing connection to Stash at {url}...")
        with httpx.Client(
            verify=runtime.STASH_VERIFY_TLS,
            headers=_auth_headers(),
            timeout=5,
            follow_redirects=True,
        ) as client:
            resp = client.post(url, json={"query": "{ version { version } }"})
            resp.raise_for_status()
            v = resp.json().get("data", {}).get("version", {}).get("version", "unknown")
            runtime.STASH_VERSION = v
            runtime.STASH_CONNECTED = True
            logger.info(f"✅ Connected to Stash! Version: {v}")
            return True
    except Exception as e:
        runtime.STASH_CONNECTED = False
        logger.error(f"❌ Failed to connect to Stash: {e}")
        logger.error("Please check STASH_URL and authentication in your config.")
        return False


def check_stash_connection_cached() -> bool:
    """TTL-cached sync variant — callers in async context use asyncio.to_thread."""
    return _status_cache.get("stash_up", producer=check_stash_connection)


async def stash_query(query: str, variables: Dict[str, Any] = None, retries: int = None) -> Dict[str, Any]:
    """Execute a GraphQL query against Stash with retry logic.

    Returns the JSON response dict. On total failure returns
    ``{"errors": [...], "data": {}}`` so callers never get None.
    """
    if retries is None:
        retries = runtime.STASH_RETRIES

    client = _get_async_client()
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = await client.post(
                _graphql_url(),
                json={"query": query, "variables": variables or {}},
                timeout=runtime.STASH_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()
            if "errors" in result and result["errors"]:
                error_msgs = [e.get("message", str(e)) for e in result["errors"]]
                logger.warning(f"GraphQL errors in response: {error_msgs}")
            return result
        except httpx.TimeoutException as e:
            last_error = e
            logger.warning(f"Stash API timeout (attempt {attempt + 1}/{retries + 1}): {e}")
            if attempt < retries:
                await asyncio.sleep(1 * (attempt + 1))
        except httpx.ConnectError as e:
            last_error = e
            logger.warning(f"Stash API connection error (attempt {attempt + 1}/{retries + 1}): {e}")
            if attempt < retries:
                await asyncio.sleep(2 * (attempt + 1))
        except httpx.HTTPStatusError as e:
            last_error = e
            logger.error(f"Stash API HTTP error: {e}")
            if 400 <= e.response.status_code < 500:
                break
            if attempt < retries:
                await asyncio.sleep(1 * (attempt + 1))
        except Exception as e:
            last_error = e
            logger.error(f"Stash API Query Error: {e}")
            break

    logger.error(f"Stash API failed after {retries + 1} attempts: {last_error}")
    return {"errors": [str(last_error)], "data": {}}


async def fetch_from_stash(
    url: str,
    extra_headers: Dict[str, str] = None,
    timeout: int = 30,
    stream: bool = False,
) -> Tuple[bytes, str, Dict[str, str]]:
    """Fetch binary content (images, subtitles) from Stash.

    Returns (data, content_type, response_headers). The `stream` parameter is
    kept for API compatibility but is ignored — httpx always buffers via
    .content for non-streaming callers. Video streaming uses the client
    directly via ``send(..., stream=True)``.
    """
    client = _get_async_client()
    headers = extra_headers or {}
    try:
        resp = await client.get(url, headers=headers, timeout=timeout)
        content_type = resp.headers.get("content-type", "application/octet-stream")
        if "text/html" in content_type:
            preview = resp.content[:200].decode("utf-8", errors="ignore")
            logger.error(f"Got HTML response instead of media from {url}")
            logger.error(f"First 200 chars: {preview}")
            raise Exception("Authentication failed - received HTML instead of media")
        resp.raise_for_status()
        resp_headers = dict(resp.headers)
        data = resp.content
        logger.debug(f"Fetch success from {url}: {len(data)} bytes, type={content_type}")
        return data, content_type, resp_headers
    except httpx.RequestError as e:
        logger.error(f"Request failed for {url}: {e}")
        raise
