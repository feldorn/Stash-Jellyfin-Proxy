"""Stash GraphQL client — the only module that talks to Stash directly.

Reads connection config + session state from proxy.runtime. Writes
STASH_SESSION / STASH_VERSION / STASH_CONNECTED / GRAPHQL_URL back to
runtime (runtime is the single source of truth; this module doesn't keep
its own copies).

Sync-only today (uses the `requests` library). A full httpx+async
migration is a post-v7.00 follow-on noted in plan §13.
"""
import logging
import time
from typing import Any, Dict

import requests

from proxy import runtime
from proxy.cache.ttl import TTLCache

logger = logging.getLogger("stash-jellyfin-proxy")

# TTL cache used by check_stash_connection_cached. Local to the client
# so invalidation (and future additions for other health probes) live
# alongside the consumer.
_status_cache = TTLCache(ttl_seconds=30.0)


def _graphql_url() -> str:
    """Build the GraphQL URL from current runtime values. Called per-request
    so hot-reloads of STASH_URL / STASH_GRAPHQL_PATH take effect without a
    restart. Cached on runtime for convenience."""
    url = f"{runtime.STASH_URL.rstrip('/')}{runtime.STASH_GRAPHQL_PATH}"
    runtime.GRAPHQL_URL = url
    return url


def get_stash_session():
    """Get or create a Stash session with ApiKey authentication.
    Session lives on `runtime.STASH_SESSION` so extracted modules share
    one connection pool."""
    if runtime.STASH_SESSION is not None:
        return runtime.STASH_SESSION

    session = requests.Session()
    session.verify = runtime.STASH_VERIFY_TLS
    if not runtime.STASH_VERIFY_TLS:
        # Suppress InsecureRequestWarning when TLS verification is disabled
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.info("TLS verification disabled (STASH_VERIFY_TLS=false)")

    if runtime.STASH_API_KEY:
        session.headers["ApiKey"] = runtime.STASH_API_KEY
        logger.info(f"Session configured with ApiKey header (key length: {len(runtime.STASH_API_KEY)})")
    else:
        logger.warning("No STASH_API_KEY configured - images will fail to load!")
        logger.warning("Add STASH_API_KEY to your config file (get from Stash -> Settings -> Security)")

    runtime.STASH_SESSION = session
    return session


def check_stash_connection() -> bool:
    """Verify we can talk to Stash. Updates runtime.STASH_VERSION and
    runtime.STASH_CONNECTED as a side effect."""
    try:
        url = _graphql_url()
        logger.info(f"Testing connection to Stash at {url}...")
        session = get_stash_session()
        resp = session.post(
            url,
            json={"query": "{ version { version } }"},
            timeout=5,
        )
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
    """TTL-cached variant of check_stash_connection for request-path callers.

    Dashboard polling used to freeze briefly during Infuse stream starts
    because every poll issued a fresh Stash GraphQL query, which can block
    behind busy sync work on the event loop. Cache the result for 30s so
    the dashboard reflects live state without spamming Stash."""
    return _status_cache.get("stash_up", producer=check_stash_connection)


def stash_query(query: str, variables: Dict[str, Any] = None, retries: int = None) -> Dict[str, Any]:
    """Execute a GraphQL query against Stash with retry logic.

    Returns the JSON response dict (with 'data' and/or 'errors' keys). On
    total failure after all retries, returns `{"errors": [...], "data": {}}`
    so callers never get a None.
    """
    if retries is None:
        retries = runtime.STASH_RETRIES

    last_error = None
    for attempt in range(retries + 1):
        try:
            session = get_stash_session()
            resp = session.post(
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
        except requests.exceptions.Timeout as e:
            last_error = e
            logger.warning(f"Stash API timeout (attempt {attempt + 1}/{retries + 1}): {e}")
            if attempt < retries:
                time.sleep(1 * (attempt + 1))
        except requests.exceptions.ConnectionError as e:
            last_error = e
            logger.warning(f"Stash API connection error (attempt {attempt + 1}/{retries + 1}): {e}")
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
        except requests.exceptions.HTTPError as e:
            last_error = e
            logger.error(f"Stash API HTTP error: {e}")
            if hasattr(e, 'response') and e.response is not None and 400 <= e.response.status_code < 500:
                break
            if attempt < retries:
                time.sleep(1 * (attempt + 1))
        except Exception as e:
            last_error = e
            logger.error(f"Stash API Query Error: {e}")
            break

    logger.error(f"Stash API failed after {retries + 1} attempts: {last_error}")
    return {"errors": [str(last_error)], "data": {}}
