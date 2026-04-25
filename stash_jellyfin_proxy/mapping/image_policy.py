"""Image format policy — per-client poster format and performer item type.

Phase 2: policy is driven by the resolved player profile (config-loaded
[player.*] sections). Profile resolution happens in
`stash_jellyfin_proxy.players.matcher.resolve_from_request(request)`.

Phase 3 will flip Swiftfin's poster_format to actually crop to portrait
(the crop_to_portrait helper in util/images.py). Today it just returns the
format string; the image endpoint decides what to do with it.
"""
from stash_jellyfin_proxy.players.matcher import resolve_from_request


def scene_poster_format(request) -> str:
    """Return 'portrait' or 'landscape' for scene poster images."""
    return resolve_from_request(request).poster_format


def performer_item_type(request) -> str:
    """Return the Jellyfin Type string for performer items (Person/BoxSet)."""
    return resolve_from_request(request).performer_type


def playlist_collection_type(request) -> str:
    """Return the CollectionType for the Playlists library tile.

    Native ("playlists") for clients that render the type — Infuse, Jellyfin
    web. Compat ("movies") for clients that don't (Swiftfin, SenPlayer); they
    can still browse and play, but have no native playlist-edit UI."""
    return "playlists" if resolve_from_request(request).playlist_native else "movies"


def playlist_item_type(request) -> str:
    """Return the Jellyfin Type string for individual playlist items.

    "Playlist" for native renderers, "BoxSet" for compat clients — both shape
    a folder of scenes; Swiftfin/SenPlayer's BoxSet view navigates correctly
    while their Playlist view doesn't exist."""
    return "Playlist" if resolve_from_request(request).playlist_native else "BoxSet"
