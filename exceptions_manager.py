"""
Mud Music Manager - Exceptions Manager

Lets Michael manually pin which YouTube video gets used for a specific
track within a specific playlist -- overriding the normal tiered search
(youtube_matcher.py) entirely for that track. This is the safety net
the matching tiers were always designed to end at (Tier 4: no match ->
Exceptions, per the design doc), and it's also usable proactively to
fix a track you already know got a bad automatic match.

Exceptions are stored in exceptions.json, keyed by playlist Spotify ID,
then by track Spotify ID -> the YouTube video ID to use instead of
running the normal search. Playlist-specific, per the design doc's
original spec, since the same song could reasonably warrant different
handling in different playlists in principle (even though in practice
it'll usually be set once and apply consistently).

Checklist item 3 (crash / interruption failsafes): exceptions.json
writes are now atomic (see _atomic_write_json()) -- a process killed
mid-write can no longer leave a corrupted or truncated file behind.
"""

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

import app_paths
import logger
import track_failures

EXCEPTIONS_FILENAME = "exceptions.json"
# v0.18.0: in the user's data folder (app_paths.DATA_DIR), not the app folder.
EXCEPTIONS_PATH = app_paths.data_path(EXCEPTIONS_FILENAME)

_YOUTUBE_ID_PATTERN = re.compile(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})")


def _atomic_write_json(path, data):
    """
    Write JSON to `path` without ever leaving a half-written or
    corrupted file behind if the process is killed mid-write
    (Checklist item 3 -- this used to be a direct overwrite). See
    config_manager._atomic_write_json() for the identical pattern used
    there -- duplicated here rather than shared, in keeping with this
    project's existing preference for independently-testable modules.

    Writes to a temp file in the same directory, flushes and fsyncs
    it, then swaps it into place with os.replace() -- a single atomic
    filesystem operation on both Windows and POSIX. The previous good
    version is also kept alongside as a `.bak` file, best-effort.
    """
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    bak_path = path.with_suffix(path.suffix + ".bak")

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())

    if path.exists():
        try:
            shutil.copy2(path, bak_path)
        except OSError as e:
            logger.log_error(f"Couldn't refresh backup copy of {path.name}: {e}")

    os.replace(tmp_path, path)


def extract_youtube_id(url_or_id: str) -> Optional[str]:
    """
    Accept a full YouTube URL (youtube.com or youtu.be) or a bare
    11-character video ID and return just the ID. Returns None if
    nothing recognizable is found.
    """
    url_or_id = url_or_id.strip()

    match = _YOUTUBE_ID_PATTERN.search(url_or_id)
    if match:
        return match.group(1)

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id):
        return url_or_id

    return None


def load_exceptions() -> dict:
    """Load exceptions.json, creating an empty one if it doesn't exist yet."""
    if not os.path.exists(EXCEPTIONS_PATH):
        save_exceptions({})
        return {}

    try:
        with open(EXCEPTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.log_error(f"Failed to read {EXCEPTIONS_FILENAME}: {e}")
        raise SystemExit(
            f"Could not read {EXCEPTIONS_FILENAME} -- it may be corrupted. "
            f"Check logs/error_log.txt for details."
        )


def save_exceptions(data: dict) -> None:
    """Write the exceptions dict to disk. Hardened against a mid-write
    crash (Checklist item 3) -- see _atomic_write_json()."""
    try:
        _atomic_write_json(EXCEPTIONS_PATH, data)
    except OSError as e:
        logger.log_error(f"Failed to write {EXCEPTIONS_FILENAME}: {e}")
        raise


def get_exception(playlist_spotify_id: str, track_spotify_id: str) -> Optional[str]:
    """Return the overridden YouTube video ID for this track in this
    playlist, or None if no exception is set."""
    data = load_exceptions()
    return data.get(playlist_spotify_id, {}).get(track_spotify_id)


def add_exception(playlist_spotify_id: str, track_spotify_id: str, youtube_url_or_id: str) -> str:
    """
    Set (or replace) the YouTube video to use for a specific track
    within a specific playlist, overriding the normal search from then
    on. Returns the normalized video ID that was saved.

    Raises ValueError if youtube_url_or_id isn't recognizable as a
    YouTube link or video ID.
    """
    video_id = extract_youtube_id(youtube_url_or_id)
    if not video_id:
        raise ValueError("Could not recognize that as a YouTube link or video ID.")

    data = load_exceptions()
    data.setdefault(playlist_spotify_id, {})[track_spotify_id] = video_id
    save_exceptions(data)
    logger.log_event(
        f"Set exception for track {track_spotify_id} in playlist "
        f"{playlist_spotify_id} -> {video_id}"
    )
    # v0.16.27 (Checklist item 28): a new link starts the track's failed
    # attempts from zero (never raises).
    track_failures.clear(playlist_spotify_id, track_spotify_id)
    return video_id


def remove_exception(playlist_spotify_id: str, track_spotify_id: str) -> bool:
    """
    Remove an exception if one exists. Returns True if something was
    actually removed, False if there was nothing to remove.
    """
    data = load_exceptions()
    playlist_exceptions = data.get(playlist_spotify_id)
    if not playlist_exceptions or track_spotify_id not in playlist_exceptions:
        return False

    del playlist_exceptions[track_spotify_id]
    if not playlist_exceptions:
        del data[playlist_spotify_id]
    save_exceptions(data)
    logger.log_event(f"Removed exception for track {track_spotify_id} in playlist {playlist_spotify_id}")
    return True


def list_exceptions(playlist_spotify_id: str) -> dict:
    """Return {track_spotify_id: youtube_video_id} for everything
    currently overridden in this playlist."""
    data = load_exceptions()
    return data.get(playlist_spotify_id, {})
