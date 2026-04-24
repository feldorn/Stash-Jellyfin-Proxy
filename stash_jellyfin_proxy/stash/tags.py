"""Tag-related Stash GraphQL helpers — look up or create tags by name."""
import logging

from .client import stash_query

logger = logging.getLogger("stash-jellyfin-proxy")


# Cached tag-id lookups so the favorite path doesn't re-query per request.
_tag_id_cache: dict = {}


async def get_or_create_tag(tag_name: str):
    """Return the Stash tag ID for `tag_name`, creating it if absent.
    Caches the result per process so the favorite toggle doesn't requery
    Stash on every invocation."""
    if not tag_name:
        return None
    if tag_name in _tag_id_cache:
        return _tag_id_cache[tag_name]
    try:
        q = """query FindTags($name: String!) { findTags(tag_filter: {name: {value: $name, modifier: EQUALS}}) { tags { id name } } }"""
        res = await stash_query(q, {"name": tag_name})
        tags = res.get("data", {}).get("findTags", {}).get("tags", [])
        if tags:
            _tag_id_cache[tag_name] = tags[0]["id"]
            return _tag_id_cache[tag_name]
        q = """mutation TagCreate($input: TagCreateInput!) { tagCreate(input: $input) { id name } }"""
        res = await stash_query(q, {"input": {"name": tag_name}})
        tag = res.get("data", {}).get("tagCreate")
        if tag:
            _tag_id_cache[tag_name] = tag["id"]
            logger.info(f"Created tag '{tag_name}' with ID {tag['id']}")
            return tag["id"]
    except Exception as e:
        logger.error(f"Error getting/creating tag '{tag_name}': {e}")
    return None
