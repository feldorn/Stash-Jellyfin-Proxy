"""Jellyfin GUID ↔ Stash numeric ID conversion helpers.

Jellyfin clients address items by GUID strings (pseudo-UUIDs). Stash
identifies entities by numeric IDs. The proxy encodes the numeric ID
as a zero-padded 32-hex string carved into UUID shape, sometimes also
prefixed (scene-123, studio-456, performer-789 etc) for type dispatch.
"""


def make_guid(numeric_id) -> str:
    """Convert a numeric ID to a GUID-like format that Jellyfin clients expect."""
    padded = str(numeric_id).zfill(32)
    return f"{padded[:8]}-{padded[8:12]}-{padded[12:16]}-{padded[16:20]}-{padded[20:32]}"


def extract_numeric_id(guid_id: str) -> str:
    """Extract numeric ID from a GUID format, or return as-is if already numeric."""
    if "-" in guid_id:
        numeric = guid_id.replace("-", "").lstrip("0")
        return numeric if numeric else "0"
    return guid_id


def get_numeric_id(item_id: str) -> str:
    """Extract numeric ID from various formats. Preserves pre-Phase-0.6
    behavior exactly: only scene- and studio- prefixes are stripped; other
    prefixed IDs (performer-, group-, tag-) fall through to
    extract_numeric_id which collapses dashes."""
    if item_id.startswith("scene-"):
        return item_id.replace("scene-", "")
    elif item_id.startswith("studio-"):
        return item_id.replace("studio-", "")
    elif "-" in item_id:
        return extract_numeric_id(item_id)
    return item_id
