"""
Mud Music Manager - UI Settings & Cache (v0.16.0)

Small JSON-backed store for everything the new GUI needs to remember
that does NOT belong in playlists.json / exceptions.json (those two
files stay exactly as they were -- the GUI never adds keys to them):

  - theme: "dark" (default) or "light" (Settings page toggle)
  - cmd_mode: what the CMD button does -- "window" (in-app console
    window) or "terminal" (a real OS terminal running the classic menu)
  - playlist_icons: custom thumbnail image per playlist (the default is
    a generated placeholder made from the playlist's name)
  - playlist_cache: last-known per-playlist numbers (tracks, length,
    exceptions, pending) so the main menu can show something sensible
    instantly on launch and when there is no internet connection. The
    app still runs a background Scan on every launch to refresh these.
  - last_scan_at: when the last successful scan finished
  - scan_on_launch (v0.16.17): whether the GUI runs that background
    Scan every launch (default True). Off, the cached numbers are shown
    until a Scan or Sync is run by hand.
  - smooth_scroll (v0.17.4): whether lists glide when scrolled (mouse
    wheel, keyboard, scroll-to-fit; default True). Off, they jump as Qt
    scrolls by itself. Read live by ui_widgets.smooth_scrolling_on().
  - keymap (v0.16.26): the keyboard keys changed in Settings -> Remap
    keyboard controls, as {screen: {action: [keys]}}; only what differs
    from the defaults is stored (ui_keys.load_keymap() merges and checks it).
  - youtube_cookies (v0.16.42): YouTube sign-in for the signed-in retry,
    {mode: off / browser / file, browser, file}. Read by the engine
    (downloader, through youtube_cookies.py), so a sync from the terminal
    menu uses it too; youtube_cookies.get_setting() checks the values.
  - edit_sections_collapsed (v0.16.43): which of the Edit page's two lists
    are closed, {"exceptions": bool, "issues": bool} (missing = open).
  - download_settings (v0.16.44): Settings -> Download quality,
    {file_format (v0.16.45), source, bitrate, cover_format (v0.17.3),
    speed_limit, pause}. Read by the engine (downloader,
    through download_settings.py), so terminal-menu syncs use it too;
    download_settings.get_setting() checks the values. The defaults are
    the app's original download behaviour, except the source (v0.17.2:
    "aac", copied without re-encoding; was "best").
  - download_source_default_v0172 (v0.17.2): True once a stored source
    "best" has been switched to the new "aac" default (done once, by
    download_settings._migrate_source_default()).
  - sync_timing (v0.17.6): averages from past Syncs that give the Sync
    Progress panel's ETA a starting point, {download_secs, download_n,
    overhead_secs, overhead_n}: seconds per download and per playlist
    (reading it, comparing, removing), with how many samples each average
    holds (capped, so it follows recent Syncs). Kept by
    ui_backend.SyncEstimate.
  - prefer_explicit (v0.17.7): Settings -> Prefer explicit versions
    (default False, the usual search). On, the YouTube search picks a
    song's explicit version over its clean one when both are found. Read
    by the engine (youtube_matcher.prefer_explicit()), so terminal-menu
    syncs use it too.
  - playlist_files (v0.24.0, Checklist item 64): Settings -> Playlist
    files (default True). On, every Sync writes the playlist's .m3u8 in
    its folder; Off, the app's .m3u8 files are removed at each playlist's
    next Sync. Read by the engine (playlist_file.enabled()), so
    terminal-menu syncs use it too.
  - scheduled_sync (v0.25.0, Checklist item 65): Settings -> Scheduled
    sync, {enabled (default False), time "HH:MM" (default "03:00"),
    notify (default True)}; scheduled_sync.get_setting() checks the
    values. scheduled_sync_last: the last run's {at, summary, ok, in_app}
    (this computer only, like sync_timing).
  - device_copy_last (v0.26.0, Checklist item 66): the folder Copy to
    Device last copied to, where its folder picker starts (this computer
    only).

Export / Import (v0.17.8, Checklist item 59): export_values() and
import_values() give user_data.py every setting and replace them all at
once; playlist_cache, last_scan_at and sync_timing describe this computer
and are never exported (user_data.DEVICE_SETTINGS).

Deliberately has NO Qt dependency, so the classic terminal menu
(main.py) can read and change the same settings the GUI uses.

Playlists are keyed by "<platform>:<source id>" ("spotify:album:<id>" for
an album, v0.16.48; "ytmusic:<id>" / "ytmusic:album:<id>" on YT Music,
v0.16.51) (not by display name),
so renaming a playlist never orphans its icon or cached numbers.
Changing a playlist's link (v0.16.12) changes its key, so
playlist_source_changed() moves the icon to the new key and drops the
old playlist's cached numbers.

Since v0.18.0 ui_settings.json and playlist_icons/ are in the user's data
folder (app_paths.DATA_DIR), like the other data files.

Writes are atomic (temp file + os.replace), same pattern as the config
files (see config_manager._atomic_write_json()). These are only UI
preferences, so an unreadable settings file is set aside as
`.corrupt` and defaults are used -- it never stops the app launching.
"""

import copy
import json
import os
import threading
import time
from typing import Any, Optional

import app_paths
import logger

# v0.18.0: both in the user's data folder (app_paths.DATA_DIR), not the app folder.
SETTINGS_FILENAME = "ui_settings.json"
SETTINGS_PATH = app_paths.data_path(SETTINGS_FILENAME)
ICONS_DIR = app_paths.data_path("playlist_icons")

THEME_DARK = "dark"
THEME_LIGHT = "light"
CMD_WINDOW = "window"
CMD_TERMINAL = "terminal"

_DEFAULTS = {
    "theme": THEME_DARK,
    "cmd_mode": CMD_WINDOW,
    "playlist_icons": {},
    "playlist_cache": {},
    "last_scan_at": None,
    "scan_on_launch": True,
    "smooth_scroll": True,
    "prefer_explicit": False,
    "playlist_files": True,
    "scheduled_sync": {"enabled": False, "time": "03:00", "notify": True},
    "scheduled_sync_last": None,
    "device_copy_last": "",
    "keymap": {},
    "youtube_cookies": {"mode": "off", "browser": "firefox", "file": ""},
    "edit_sections_collapsed": {},
    "download_settings": {"file_format": "m4a", "source": "aac", "bitrate": "auto", "cover_format": "png",
                          "speed_limit": "off", "pause": "off"},
    "sync_timing": {},
}

_lock = threading.RLock()
_data: Optional[dict] = None


def _load() -> dict:
    global _data
    if _data is not None:
        return _data

    data = {}
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("settings file is not a JSON object")
        except (OSError, ValueError) as e:
            logger.log_error(f"Couldn't read {SETTINGS_FILENAME} ({e}) -- using defaults.")
            try:
                os.replace(SETTINGS_PATH, SETTINGS_PATH + ".corrupt")
            except OSError:
                pass
            data = {}

    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEFAULTS.items()}
    merged.update(data)
    # A hand-edited / older file could hold a bad value -- fall back per key.
    if merged["theme"] not in (THEME_DARK, THEME_LIGHT):
        merged["theme"] = THEME_DARK
    if merged["cmd_mode"] not in (CMD_WINDOW, CMD_TERMINAL):
        merged["cmd_mode"] = CMD_WINDOW
    if not isinstance(merged["scan_on_launch"], bool):
        merged["scan_on_launch"] = True
    if not isinstance(merged["smooth_scroll"], bool):
        merged["smooth_scroll"] = True
    if not isinstance(merged["prefer_explicit"], bool):
        merged["prefer_explicit"] = False
    if not isinstance(merged["playlist_files"], bool):
        merged["playlist_files"] = True
    for key in ("playlist_icons", "playlist_cache", "keymap", "youtube_cookies", "edit_sections_collapsed",
                "download_settings", "sync_timing", "scheduled_sync"):
        if not isinstance(merged[key], dict):
            merged[key] = {}
    _data = merged
    return _data


def _write(data: dict) -> None:
    """Atomic write; raises OSError (v0.17.8: split out of _save() for
    import_values(), which must know when it failed)."""
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, SETTINGS_PATH)


def _save() -> None:
    try:
        _write(_load())
    except OSError as e:
        logger.log_error(f"Failed to write {SETTINGS_FILENAME}: {e}")


def get(key: str) -> Any:
    with _lock:
        return _load().get(key)


def set_value(key: str, value: Any) -> None:
    with _lock:
        _load()[key] = value
        _save()


def export_values() -> dict:
    """Every setting, as a deep copy (Settings -> Export, v0.17.8,
    user_data.py)."""
    with _lock:
        return copy.deepcopy(_load())


def import_values(values: dict, keep=(), clear=()) -> None:
    """Replace every setting with `values` (Settings -> Import, v0.17.8):
    keys in `keep` stay as they are here, keys in `clear` go back to their
    defaults. The result is checked as on launch (bad values fall back per
    key). Raises OSError if the file can't be written; the settings in use
    are then unchanged."""
    global _data
    with _lock:
        current = _load()
        new = {k: copy.deepcopy(v) for k, v in values.items() if k not in keep}
        for k in keep:
            if k in current:
                new[k] = copy.deepcopy(current[k])
        for k in clear:
            new.pop(k, None)
        _write(new)
        _data = None
        _load()


# ---------------------------------------------------------------------------
# Per-playlist helpers
# ---------------------------------------------------------------------------

def playlist_key(entry: dict) -> str:
    """Stable key for a playlist: '<platform>:<source id>'. Survives a
    rename (the display name is never part of it). A Spotify album
    (v0.16.48, Checklist item 41a) is 'spotify:album:<album id>'; a YT
    Music playlist / album (v0.16.51) 'ytmusic:<id>' / 'ytmusic:album:<id>'."""
    platform = entry.get("platform", "spotify")
    if platform == "youtube":
        source_id = entry.get("youtube_playlist_id")
    elif platform == "ytmusic":
        if entry.get("kind") == "album":
            return f"ytmusic:album:{entry.get('ytmusic_album_id')}"
        source_id = entry.get("ytmusic_playlist_id")
    elif platform in ("applemusic", "amazonmusic"):
        # v0.16.53: 'applemusic:us/pl.x' / 'applemusic:album:us/123'; v0.16.54:
        # 'amazonmusic:music.amazon.com/B0..' / 'amazonmusic:album:music.amazon.com/B0..'
        if entry.get("kind") == "album":
            return f"{platform}:album:{entry.get(platform + '_album')}"
        source_id = entry.get(platform + "_playlist")
    elif platform == "soundcloud":  # v0.16.52: 'soundcloud:<set path>' / 'soundcloud:album:<set path>'
        if entry.get("kind") == "album":
            return f"soundcloud:album:{entry.get('soundcloud_set')}"
        source_id = entry.get("soundcloud_set")
    elif entry.get("kind") == "album":
        return f"{platform}:album:{entry.get('spotify_album_id')}"
    else:
        source_id = entry.get("spotify_id")
    return f"{platform}:{source_id}"


def get_cache(key: str) -> dict:
    with _lock:
        return dict(_load()["playlist_cache"].get(key, {}))


def update_cache(key: str, **fields) -> None:
    with _lock:
        cache = _load()["playlist_cache"].setdefault(key, {})
        cache.update(fields)
        _save()


def forget_playlist(key: str) -> None:
    """Drop cached numbers for a playlist that was removed. A custom
    icon file is left on disk (cheap, and a re-add can pick it up)."""
    with _lock:
        data = _load()
        data["playlist_cache"].pop(key, None)
        _save()


def playlist_source_changed(old_key: str, new_key: str) -> None:
    """The playlist's link changed (Edit page, Checklist item 16), so its
    key changed too. The custom picture follows the playlist to the new
    key; the cached numbers for BOTH keys are dropped, because they
    describe the old playlist (or a stale earlier visit to the new one).
    The next scan fills them in again."""
    if old_key == new_key:
        return
    with _lock:
        data = _load()
        icon = data["playlist_icons"].pop(old_key, None)
        if icon:
            data["playlist_icons"][new_key] = icon
        data["playlist_cache"].pop(old_key, None)
        data["playlist_cache"].pop(new_key, None)
        _save()


def get_icon(key: str) -> Optional[str]:
    with _lock:
        path = _load()["playlist_icons"].get(key)
    if path and os.path.exists(path):
        return path
    return None


def set_icon(key: str, path: Optional[str]) -> None:
    with _lock:
        icons = _load()["playlist_icons"]
        if path:
            icons[key] = path
        else:
            icons.pop(key, None)
        _save()


def mark_scanned_now() -> float:
    now = time.time()
    set_value("last_scan_at", now)
    return now
