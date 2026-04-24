"""Jellyfin UserDto builder.

Strongly-typed clients (Fladder, OpenAPI-generated Dart SDKs) reject
UserDto responses that miss `Policy` or `Configuration` fields with a
generic "unable to connect to host" error. The full schema below mirrors
Jellyfin 10.10.x and has been validated against those clients — do not
prune fields without testing.
"""
from stash_jellyfin_proxy import runtime


def build_user_dto(username=None) -> dict:
    """Build a complete Jellyfin UserDto from runtime config."""
    return {
        "Name": username or runtime.SJS_USER or "Stash User",
        "ServerId": runtime.SERVER_ID,
        "Id": _user_id(),
        "HasPassword": True,
        "HasConfiguredPassword": True,
        "HasConfiguredEasyPassword": False,
        "EnableAutoLogin": False,
        "LastLoginDate": "2024-01-01T00:00:00.0000000Z",
        "LastActivityDate": "2024-01-01T00:00:00.0000000Z",
        "PrimaryImageTag": "",
        "Policy": {
            "IsAdministrator": True,
            "IsHidden": False,
            "IsDisabled": False,
            "MaxParentalRating": None,
            "BlockedTags": [],
            "AllowedTags": [],
            "EnableUserPreferenceAccess": True,
            "AccessSchedules": [],
            "BlockUnratedItems": [],
            "EnableRemoteControlOfOtherUsers": False,
            "EnableSharedDeviceControl": True,
            "EnableRemoteAccess": True,
            "EnableLiveTvManagement": False,
            "EnableLiveTvAccess": False,
            "EnableContentDeletion": False,
            "EnableContentDeletionFromFolders": [],
            "EnableContentDownloading": True,
            "EnableSyncTranscoding": True,
            "EnableMediaConversion": False,
            "EnabledDevices": [],
            "EnableAllDevices": True,
            "EnabledChannels": [],
            "EnableAllChannels": True,
            "EnabledFolders": [],
            "EnableAllFolders": True,
            "InvalidLoginAttemptCount": 0,
            "LoginAttemptsBeforeLockout": -1,
            "MaxActiveSessions": 0,
            "EnablePlaybackRemuxing": True,
            "ForceRemoteSourceTranscoding": False,
            "EnableMediaPlayback": True,
            "EnableAudioPlaybackTranscoding": True,
            "EnableVideoPlaybackTranscoding": True,
            "EnablePublicSharing": True,
            "RemoteClientBitrateLimit": 0,
            "AuthenticationProviderId": "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider",
            "PasswordResetProviderId": "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider",
            "SyncPlayAccess": "CreateAndJoinGroups",
            "EnableCollectionManagement": False,
            "EnableSubtitleManagement": False,
            "EnableLyricManagement": False,
        },
        "Configuration": {
            "PlayDefaultAudioTrack": True,
            "SubtitleLanguagePreference": "",
            "DisplayMissingEpisodes": False,
            "GroupedFolders": [],
            "SubtitleMode": "Default",
            "DisplayCollectionsView": False,
            "EnableLocalPassword": False,
            "OrderedViews": [],
            "LatestItemsExcludes": [],
            "MyMediaExcludes": [],
            "HidePlayedInLatest": True,
            "RememberAudioSelections": True,
            "RememberSubtitleSelections": True,
            "EnableNextEpisodeAutoPlay": True,
            "CastReceiverId": "",
        },
    }


def _user_id() -> str:
    """Derive the stable per-user UUID the monolith sets at boot."""
    import uuid
    base = runtime.SERVER_ID.replace("-", "").ljust(32, "0")[:32]
    return str(uuid.uuid5(uuid.UUID(base), runtime.SJS_USER or "user"))
