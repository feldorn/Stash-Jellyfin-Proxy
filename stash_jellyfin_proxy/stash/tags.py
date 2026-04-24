"""Tag-related Stash GraphQL helpers — look up or create tags by name."""
import logging

from .client import stash_query

logger = logging.getLogger("stash-jellyfin-proxy")


# Cached tag-id lookups so hot paths don't re-query per request.
# Key is lowercased so case variants share a cache entry.
_tag_id_cache: dict = {}


async def get_or_create_tag(tag_name: str):
    """Return the Stash tag ID for `tag_name`, creating it if absent.

    Lookup is case-insensitive: if the user has 'Series' but the config
    says 'SERIES', we match the existing tag rather than creating a
    duplicate. Create always uses the config's exact casing."""
    if not tag_name:
        return None
    cache_key = tag_name.lower()
    if cache_key in _tag_id_cache:
        return _tag_id_cache[cache_key]
    try:
        # `q` is Stash's fuzzy search — cheap, returns substring matches.
        # We filter client-side for case-insensitive exact match.
        q = """query FindTags($q: String!) { findTags(filter: {q: $q, per_page: 50}) { tags { id name } } }"""
        res = await stash_query(q, {"q": tag_name})
        tags = res.get("data", {}).get("findTags", {}).get("tags", [])
        for t in tags:
            if (t.get("name") or "").lower() == cache_key:
                _tag_id_cache[cache_key] = t["id"]
                return t["id"]

        # Not found → create with the caller's exact casing.
        create_q = """mutation TagCreate($input: TagCreateInput!) { tagCreate(input: $input) { id name } }"""
        res = await stash_query(create_q, {"input": {"name": tag_name}})
        tag = res.get("data", {}).get("tagCreate")
        if tag:
            _tag_id_cache[cache_key] = tag["id"]
            logger.info(f"Created tag '{tag_name}' with ID {tag['id']}")
            return tag["id"]
    except Exception as e:
        logger.error(f"Error getting/creating tag '{tag_name}': {e}")
    return None
