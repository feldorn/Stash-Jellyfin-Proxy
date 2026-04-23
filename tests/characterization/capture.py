#!/usr/bin/env python3
"""Record proxy responses as characterization fixtures.

Picks a deterministic sample of scenes / studios / performers / groups /
tags from a running dev proxy, hits the representative endpoint set, and
writes one fixture JSON per request. The replay test loads these and
replays against the current proxy to detect regressions during refactor.

Run from repo root with the dev proxy up at http://192.168.0.200:18096:

    python3 tests/characterization/capture.py
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

THIS_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = THIS_DIR / "fixtures"

# Dev-stack defaults — matches the baseline dev conf.
BASE = "http://192.168.0.200:18096"
USER_ID = "bed0fa2f-5a08-543a-96eb-ef0150360506"
ACCESS_TOKEN = "a89fc0ca-e371-4023-b85f-afcf1fc7d44b"
AUTH = f'MediaBrowser Client="char-capture", Device="dev", DeviceId="char-001", Version="6.02", Token="{ACCESS_TOKEN}"'

# Sample-size targets per plan §2.1 / §12 Q1.
SAMPLE_SCENES = 20
SAMPLE_STUDIOS = 15
SAMPLE_PERFORMERS = 15
SAMPLE_GROUPS = 10
DETAIL_SUBSET = 10    # deep-dive endpoints hit this many items per type


def _request(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any, Dict[str, str]]:
    url = BASE + path
    data = None
    headers = {"Accept": "application/json", "X-Emby-Authorization": AUTH}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            status = resp.status
            # HTTPMessage headers come through urllib as a list — extract
            # case-insensitively.
            ct = resp.headers.get("Content-Type") or resp.headers.get("content-type") or ""
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        status = e.code
        ct = (e.headers.get("Content-Type") or e.headers.get("content-type") or "") if e.headers else ""
    try:
        if "application/json" in ct and raw:
            parsed = json.loads(raw)
        elif ct and not ct.startswith("application/json"):
            parsed = {"__content_type__": ct, "__bytes__": len(raw)}
        elif raw:
            # No content-type but got bytes — try json first, fall back to byte-meta.
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"__content_type__": ct, "__bytes__": len(raw)}
        else:
            parsed = {"__content_type__": ct, "__bytes__": 0}
    except ValueError:
        parsed = {"__content_type__": ct, "__raw__": raw[:200].decode("latin-1", errors="replace")}
    return status, parsed, {"content_type": ct}


def _pick_ids(url: str, prefix: str, n: int) -> List[str]:
    status, body, _ = _request("GET", url)
    items = body.get("Items", []) if isinstance(body, dict) else []
    out = []
    for it in items:
        iid = str(it.get("Id", ""))
        if iid.startswith(prefix) and iid not in out:
            out.append(iid)
            if len(out) >= n:
                break
    return out


# Endpoints whose Items[] membership is non-deterministic (random pool,
# time-window-bounded, recency-driven). For these we record the shape
# only — Items count, per-item key set — so replay detects structural
# regressions without false positives from pool churn.
SHAPE_ONLY = {
    "shows_nextup",
    "items_resume",
    "items_latest",
    "items_filters_scenes",       # random cover-image picked per request
    "items_filters_studios",
    "sessions_list",              # includes current session
    # Library root listings — TotalRecordCount drifts as Stash data
    # changes over time; care about structure, not count.
    "items_root-scenes",
    "items_root-studios",
    "items_root-performers",
    "items_root-groups",
    "items_root-tags",
}


def _write_fixture(name: str, method: str, path: str, body: Optional[Dict], status: int, response: Any, content_type: str):
    data = {
        "name": name,
        "compare_mode": "shape" if name in SHAPE_ONLY else "full",
        "request": {
            "method": method,
            "path": path,
            "body": body,
        },
        "response": {
            "status": status,
            "content_type": content_type,
            "body": response,
        },
    }
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / f"{name}.json"
    out.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))


def _capture(name: str, method: str, path: str, body: Optional[Dict] = None):
    status, resp_body, headers = _request(method, path, body)
    ct = headers.get("content_type", "")
    _write_fixture(name, method, path, body, status, resp_body, ct)
    print(f"  {name:<48} {status} ({ct[:30]})")


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base

    print(f"Capturing fixtures from {BASE} → {FIXTURE_DIR}")
    if FIXTURE_DIR.exists():
        for f in FIXTURE_DIR.glob("*.json"):
            f.unlink()

    # --- Static single-shot endpoints ---
    _capture("system_info_public", "GET", "/System/Info/Public")
    _capture("system_info", "GET", "/System/Info")
    _capture("system_endpoint", "GET", "/System/Endpoint")
    _capture("users_public", "GET", "/Users/Public")
    _capture("user_me", "GET", "/Users/Me")
    _capture("user_by_id", "GET", f"/Users/{USER_ID}")
    _capture("branding_config", "GET", "/Branding/Configuration")
    _capture("displayprefs", "GET", f"/DisplayPreferences/usersettings?userId={USER_ID}&client=emby")
    _capture("sessions_list", "GET", "/Sessions")

    # --- Views / library roots ---
    _capture("user_views", "GET", f"/Users/{USER_ID}/Views")
    _capture("user_grouping_options", "GET", f"/Users/{USER_ID}/GroupingOptions")

    # --- Library listings for every root ---
    for root in ("root-scenes", "root-studios", "root-performers", "root-groups", "root-tags"):
        _capture(f"items_{root}", "GET",
                 f"/Users/{USER_ID}/Items?ParentId={root}&Limit=25&StartIndex=0&Fields=PrimaryImageAspectRatio")
    # Also tag-groups, filters subfolder
    _capture("items_tag_cock_hero", "GET",
             f"/Users/{USER_ID}/Items?ParentId=tag-cock-hero&Limit=25&StartIndex=0&Fields=PrimaryImageAspectRatio")
    _capture("items_filters_scenes", "GET",
             f"/Users/{USER_ID}/Items?ParentId=filters-scenes&Limit=25&StartIndex=0")
    _capture("items_filters_studios", "GET",
             f"/Users/{USER_ID}/Items?ParentId=filters-studios&Limit=25&StartIndex=0")

    # --- Home rails ---
    _capture("items_resume", "GET",
             f"/Users/{USER_ID}/Items/Resume?Limit=12&Fields=PrimaryImageAspectRatio&MediaTypes=Video")
    _capture("items_latest", "GET",
             f"/Users/{USER_ID}/Items/Latest?Limit=16&Fields=PrimaryImageAspectRatio&ParentId=root-scenes")
    _capture("shows_nextup", "GET",
             f"/Shows/NextUp?Limit=24&UserId={USER_ID}&EnableResumable=false")

    # --- Search ---
    _capture("search_cum", "GET", "/Search/Hints?searchTerm=cum&Limit=10")
    _capture("search_scene", "GET", "/Search/Hints?searchTerm=scene&Limit=10&IncludeItemTypes=Movie")

    # --- Filter panel ---
    _capture("items_filters_root_scenes", "GET", "/Items/Filters?ParentId=root-scenes")

    # --- Sampled scenes: detail + playback + similar + image meta ---
    scenes = _pick_ids(
        f"/Users/{USER_ID}/Items?ParentId=root-scenes&Limit=100&StartIndex=0&IncludeItemTypes=Movie",
        "scene-", SAMPLE_SCENES,
    )
    print(f"  sampled {len(scenes)} scenes")
    for sid in scenes:
        _capture(f"scene_{sid}_detail", "GET", f"/Users/{USER_ID}/Items/{sid}")
    for sid in scenes[:DETAIL_SUBSET]:
        _capture(f"scene_{sid}_playback_get", "GET", f"/Items/{sid}/PlaybackInfo?UserId={USER_ID}")
        _capture(f"scene_{sid}_playback_post", "POST", f"/Items/{sid}/PlaybackInfo", body={
            "UserId": USER_ID,
            "MaxStreamingBitrate": 140000000,
            "DeviceProfile": {"Name": "char-capture"},
        })
        _capture(f"scene_{sid}_similar", "GET", f"/Items/{sid}/Similar?userId={USER_ID}&limit=6")
        _capture(f"scene_{sid}_intros", "GET", f"/Users/{USER_ID}/Items/{sid}/Intros")
        _capture(f"scene_{sid}_theme", "GET", f"/Items/{sid}/ThemeSongs")
        # Image metadata only (content-type + byte length); body isn't JSON
        _capture(f"scene_{sid}_image_primary", "GET", f"/Items/{sid}/Images/Primary?maxHeight=400")
        _capture(f"scene_{sid}_image_backdrop", "GET", f"/Items/{sid}/Images/Backdrop?fillHeight=400&fillWidth=711")

    # --- Sampled studios ---
    studios = _pick_ids(f"/Users/{USER_ID}/Items?ParentId=root-studios&Limit=100", "studio-", SAMPLE_STUDIOS)
    print(f"  sampled {len(studios)} studios")
    for sid in studios:
        _capture(f"studio_{sid}_detail", "GET", f"/Users/{USER_ID}/Items/{sid}")
    for sid in studios[:DETAIL_SUBSET]:
        _capture(f"studio_{sid}_items", "GET",
                 f"/Users/{USER_ID}/Items?ParentId={sid}&Limit=25&StartIndex=0")

    # --- Sampled performers ---
    performers = _pick_ids(f"/Users/{USER_ID}/Items?ParentId=root-performers&Limit=100", "performer-", SAMPLE_PERFORMERS)
    print(f"  sampled {len(performers)} performers")
    for pid in performers:
        _capture(f"performer_{pid}_detail", "GET", f"/Users/{USER_ID}/Items/{pid}")
    for pid in performers[:DETAIL_SUBSET]:
        _capture(f"performer_{pid}_items", "GET",
                 f"/Users/{USER_ID}/Items?ParentId={pid}&Limit=25&StartIndex=0")

    # --- Sampled groups ---
    groups = _pick_ids(f"/Users/{USER_ID}/Items?ParentId=root-groups&Limit=100", "group-", SAMPLE_GROUPS)
    print(f"  sampled {len(groups)} groups")
    for gid in groups:
        _capture(f"group_{gid}_detail", "GET", f"/Users/{USER_ID}/Items/{gid}")
    for gid in groups[:DETAIL_SUBSET]:
        _capture(f"group_{gid}_items", "GET",
                 f"/Users/{USER_ID}/Items?ParentId={gid}&Limit=25&StartIndex=0")

    # --- Negative tests: unknown IDs ---
    _capture("negative_unknown_scene", "GET", f"/Users/{USER_ID}/Items/scene-99999999")
    _capture("negative_unknown_parent", "GET",
             f"/Users/{USER_ID}/Items?ParentId=bogus-prefix-123&Limit=10")

    print("Done.")
    total = len(list(FIXTURE_DIR.glob("*.json")))
    print(f"{total} fixtures in {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
