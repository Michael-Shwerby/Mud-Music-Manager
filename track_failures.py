"""
Mud Music Manager - Track Failures (v0.16.27, Checklist item 28)

Remembers, per playlist and per track, why a track's last download
attempt failed and on how many syncs it has failed, so the app can tell
the user what kind of problem it is:

  - "No match": the search was fully carried out (every search tier that
    was needed returned normally) and nothing within the duration
    tolerance came back. Retrying won't change that, so it is shown in
    red straight away -- a case for an exception link.
  - Any other failure (search failed, download blocked, video
    unavailable, no connection, ...) is labelled with what went wrong,
    and shown as "Unavailable" in red once the track has failed on
    UNAVAILABLE_AFTER syncs. It is still tried on every sync either way.

Stored in track_failures.json next to playlists.json / exceptions.json:
  {playlist source id: {track key: {kind, reason, failed_syncs,
                                     last_failed, title, artist}}}
The track key is the same one exceptions_manager uses (the Spotify
Track ID, or the bare video ID for a YouTube-native playlist).
playlists.json stays per-playlist only, and the downloaded files stay
the only record of what is downloaded (design doc §3.3): this file only
ever describes tracks that are NOT downloaded.

A track's entry is removed when it downloads, when an exception is set
for it (exceptions_manager.add_exception), or when it is no longer in
the live playlist (prune(), at the end of a sync).

"Age-restricted" (v0.16.38, Checklist item 44) has its own kind and is
red ("Unavailable") straight away like "No match": YouTube only plays
such a video to a signed-in adult, so an anonymous retry never works.
Since v0.16.42 (Checklist item 48) a sync tries it once more signed in
when YouTube sign-in is on in Settings (see youtube_cookies.py); if that
fails too, it stays red.
Records saved before v0.16.38 as "Video unavailable" with an age message
are read back as Age-restricted (_upgrade()).

Failures caused by having no internet connection are recorded (so the
reason is shown) but do not count towards UNAVAILABLE_AFTER: losing the
connection mid-sync says nothing about the track itself.

load_all() / replace_all() (v0.17.8, Checklist item 59) let user_data.py
export the whole file and replace it on import.

Writes are atomic (temp file + os.replace), same pattern as the config
files (§3.8). This file is only diagnostic, so an unreadable copy is set
aside as `.corrupt` and treated as empty -- it never stops a sync.
"""

import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import logger

FAILURES_FILENAME = "track_failures.json"
FAILURES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), FAILURES_FILENAME)

# A track is shown as "Unavailable" after failing on this many syncs
# (Michael, 2026-10-07). "No match" doesn't wait for this.
UNAVAILABLE_AFTER = 5

# ---------------------------------------------------------------------------
# Failure kinds
# ---------------------------------------------------------------------------

NO_MATCH = "no_match"                    # search done, nothing the right length
SEARCH_FAILED = "search_failed"          # a needed search errored (not a connection problem)
TRACK_INFO = "track_info"                # Spotify gave no length, so nothing could be checked
DOWNLOAD_BLOCKED = "download_blocked"    # HTTP 403 / 429 from YouTube, after the retries
BOT_CHECK = "bot_check"                  # "Sign in to confirm you're not a bot"
VIDEO_UNAVAILABLE = "video_unavailable"  # private, removed, region / members only...
AGE_RESTRICTED = "age_restricted"        # "Sign in to confirm your age" (v0.16.38)
CONNECTION = "connection"                # no internet / timed out (not counted)
DOWNLOAD_ERROR = "download_error"        # any other yt-dlp / ffmpeg failure
FILE_ERROR = "file_error"                # couldn't write / move the file on disk
APP_ERROR = "app_error"                  # an unexpected error in the app's own code

LABELS = {
    NO_MATCH: "No match",
    SEARCH_FAILED: "Search failed",
    TRACK_INFO: "Track info missing",
    DOWNLOAD_BLOCKED: "Download blocked",
    BOT_CHECK: "YouTube bot check",
    VIDEO_UNAVAILABLE: "Video unavailable",
    AGE_RESTRICTED: "Age-restricted",
    CONNECTION: "No connection",
    DOWNLOAD_ERROR: "Download error",
    FILE_ERROR: "File error",
    APP_ERROR: "App error",
}

# What the user can do about each kind (tooltips and Sync Details).
HINTS = {
    NO_MATCH: "The search worked, but nothing found was the right length. Retrying won't change this: "
              "add an exception link on the Edit page.",
    SEARCH_FAILED: "YouTube's search returned an error, so the right match couldn't be checked. "
                   "Usually temporary: the next sync tries again.",
    TRACK_INFO: "Spotify didn't give this track's length, so no search result could be checked. "
                "Usually temporary: the next sync reads it again.",
    DOWNLOAD_BLOCKED: "YouTube refused the download (tried 3 times). Usually temporary: the next sync tries again.",
    BOT_CHECK: "YouTube asked to confirm this isn't a bot. Usually temporary: the next sync tries again. "
               "If it keeps happening, turn on YouTube sign-in in Settings: a refused download is then "
               "tried once more signed in with your browser's cookies.",
    VIDEO_UNAVAILABLE: "The video is private, removed, or restricted (region, members only). "
                       "Retrying won't help: add an exception link to another upload.",
    AGE_RESTRICTED: "YouTube only plays this video to signed-in adults. Turn on YouTube sign-in in "
                    "Settings (cookies from a browser signed in to YouTube) and the next sync tries it "
                    "signed in. Or add an exception link to another upload of the song "
                    "(e.g. a lyric video or a different channel) that isn't age-restricted.",
    CONNECTION: "The internet connection dropped. Not counted as a failed attempt; the next sync tries again.",
    DOWNLOAD_ERROR: "The download or the audio conversion failed. The next sync tries again.",
    FILE_ERROR: "The file couldn't be written or moved into the playlist folder (disk full, file in use, "
                "permissions?). The next sync tries again.",
    APP_ERROR: "Something unexpected went wrong in the app (details in logs/error_log.txt). "
               "The next sync tries again.",
}

# Kinds that don't count towards UNAVAILABLE_AFTER.
NOT_COUNTED = {CONNECTION}

# Status of a recorded failure, as the GUI shows it.
STATUS_NO_MATCH = "no_match"        # red at once
STATUS_UNAVAILABLE = "unavailable"  # red, UNAVAILABLE_AFTER or more failed syncs, or age-restricted
STATUS_RETRY = "retry"              # amber, still counted as pending

# ---------------------------------------------------------------------------
# Classifying error messages
# ---------------------------------------------------------------------------

_BLOCKED_PATTERNS = ("http error 403", "403: forbidden", "http error 429", "too many requests")
_BOT_PATTERNS = ("not a bot",)
_AGE_PATTERNS = ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users")
_UNAVAILABLE_PATTERNS = (
    "private video", "video unavailable", "this video is unavailable", "has been removed",
    "is no longer available", "has been terminated", "copyright", "not available in your country",
    "not made this video available", "members-only", "members only", "join this channel",
    "this live event", "premieres in", "deleted video", "video is not available",
)
_CONNECTION_PATTERNS = (
    "getaddrinfo failed", "failed to resolve", "name resolution", "nodename nor servname",
    "timed out", "timeout", "connection reset", "connection aborted", "connection refused",
    "network is unreachable", "no route to host", "remote end closed", "unable to connect",
    "max retries exceeded", "connectionerror", "connecterror", "urlopen error",
    "errno 11001", "errno 11002", "errno 11004", "winerror 10051", "winerror 10060", "winerror 10065",
)


def is_connection_error(message: str) -> bool:
    text = (message or "").lower()
    return any(p in text for p in _CONNECTION_PATTERNS)


def classify_download_error(message: str) -> str:
    """The failure kind for a yt-dlp download error message."""
    text = (message or "").lower().replace("’", "'")
    if any(p in text for p in _BLOCKED_PATTERNS):
        return DOWNLOAD_BLOCKED
    if any(p in text for p in _BOT_PATTERNS):
        return BOT_CHECK
    if is_age_restricted(text):
        return AGE_RESTRICTED
    if any(p in text for p in _UNAVAILABLE_PATTERNS):
        return VIDEO_UNAVAILABLE
    if is_connection_error(text):
        return CONNECTION
    return DOWNLOAD_ERROR


def is_age_restricted(message: str) -> bool:
    text = (message or "").lower().replace("’", "'")
    return any(p in text for p in _AGE_PATTERNS)


def classify_search_error(message: str) -> str:
    """The failure kind for a search that raised an error."""
    text = (message or "").lower().replace("’", "'")
    if any(p in text for p in _BOT_PATTERNS):
        return BOT_CHECK
    if is_connection_error(text):
        return CONNECTION
    return SEARCH_FAILED


# ---------------------------------------------------------------------------
# Describing a failure
# ---------------------------------------------------------------------------

def label(kind: Optional[str]) -> str:
    return LABELS.get(kind or "", LABELS[DOWNLOAD_ERROR])


def hint(kind: Optional[str]) -> str:
    return HINTS.get(kind or "", HINTS[DOWNLOAD_ERROR])


def status(record: Optional[dict]) -> Optional[str]:
    """STATUS_NO_MATCH / STATUS_UNAVAILABLE / STATUS_RETRY, or None for
    no record."""
    if not record:
        return None
    if record.get("kind") == NO_MATCH:
        return STATUS_NO_MATCH
    if record.get("kind") == AGE_RESTRICTED:
        return STATUS_UNAVAILABLE
    try:
        failed = int(record.get("failed_syncs") or 0)
    except (TypeError, ValueError):
        failed = 0
    return STATUS_UNAVAILABLE if failed >= UNAVAILABLE_AFTER else STATUS_RETRY


def summary(record: dict) -> str:
    """One line for the Sync Log, Sync Details and the terminal menu,
    e.g. "Download blocked: HTTP Error 403: Forbidden (tried 3 times) --
    failed on 2 of 5 syncs"."""
    kind = record.get("kind")
    text = f"{label(kind)}: {record.get('reason') or 'unknown reason'}"
    st = status(record)
    failed = int(record.get("failed_syncs") or 0)
    if st == STATUS_NO_MATCH:
        return text + " -- add an exception link"
    if kind == AGE_RESTRICTED:
        return text + (" -- turn on YouTube sign-in in Settings, or add an exception link to an upload "
                       "that isn't age-restricted")
    if st == STATUS_UNAVAILABLE:
        return f"Unavailable (failed on {failed} syncs) -- {text}"
    if kind in NOT_COUNTED:
        return text + " -- not counted as a failed attempt"
    return f"{text} -- failed on {failed} of {UNAVAILABLE_AFTER} syncs"


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

_lock = threading.RLock()


def _atomic_write_json(path, data):
    """Temp file + fsync + os.replace, as config_manager / exceptions_manager
    do (duplicated on purpose, per this project's preference for
    independent modules). A `.bak` of the previous version is kept."""
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    bak_path = path.with_suffix(path.suffix + ".bak")

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())

    if path.exists():
        try:
            shutil.copy2(path, bak_path)
        except OSError as e:
            logger.log_error(f"Couldn't refresh backup copy of {path.name}: {e}")

    os.replace(tmp_path, path)


def _load() -> dict:
    """Read from disk every time (it is small), so the GUI, a sync
    worker and a terminal-menu process never see stale data."""
    if not os.path.exists(FAILURES_PATH):
        return {}
    try:
        with open(FAILURES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError) as e:
        logger.log_error(f"Couldn't read {FAILURES_FILENAME} ({e}) -- starting it again empty.")
        try:
            os.replace(FAILURES_PATH, FAILURES_PATH + ".corrupt")
        except OSError:
            pass
        return {}


def _save(data: dict) -> None:
    try:
        _atomic_write_json(FAILURES_PATH, data)
    except OSError as e:
        logger.log_error(f"Failed to write {FAILURES_FILENAME}: {e}")


def _upgrade(record: dict) -> dict:
    """A record saved before v0.16.38 as "Video unavailable" for an
    age-restricted video reads back as Age-restricted."""
    if record.get("kind") == VIDEO_UNAVAILABLE and is_age_restricted(record.get("reason") or ""):
        record["kind"] = AGE_RESTRICTED
    return record


def load_all() -> dict:
    """Every playlist's records, as stored (Settings -> Export, v0.17.8,
    user_data.py)."""
    with _lock:
        return _load()


def replace_all(data: dict) -> None:
    """Replace every record (Settings -> Import, v0.17.8). Unlike the
    other writes here it raises OSError, so the import can say it failed."""
    with _lock:
        _atomic_write_json(FAILURES_PATH, {k: v for k, v in data.items() if isinstance(v, dict)})


def get_playlist(playlist_id: str) -> dict:
    """{track key: record} for one playlist (a copy)."""
    with _lock:
        return {k: _upgrade(dict(v)) for k, v in _load().get(playlist_id, {}).items() if isinstance(v, dict)}


def get(playlist_id: str, track_key: str) -> Optional[dict]:
    return get_playlist(playlist_id).get(track_key)


def record_failure(playlist_id: str, track_key: str, kind: Optional[str], reason: Optional[str],
                   title: str = "", artist: str = "") -> dict:
    """Record one failed attempt (one sync, or one exception fix) and
    return the updated record. Never raises."""
    kind = kind if kind in LABELS else DOWNLOAD_ERROR
    record = {"kind": kind, "reason": reason or "unknown reason", "failed_syncs": 0,
              "last_failed": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "title": title or "", "artist": artist or ""}
    try:
        with _lock:
            data = _load()
            tracks = data.setdefault(playlist_id, {})
            old = tracks.get(track_key) or {}
            try:
                failed = int(old.get("failed_syncs") or 0)
            except (TypeError, ValueError):
                failed = 0
            record["failed_syncs"] = failed if kind in NOT_COUNTED else failed + 1
            tracks[track_key] = record
            _save(data)
    except Exception as e:  # diagnostic only -- never break a sync over it
        logger.log_error(f"Couldn't record a failure for track {track_key}: {type(e).__name__}: {e}")
    return dict(record)


def clear(playlist_id: str, track_key: str) -> bool:
    """Forget a track's failures (it downloaded, or an exception was
    set). True if there was something to forget. Never raises."""
    try:
        with _lock:
            data = _load()
            tracks = data.get(playlist_id)
            if not tracks or track_key not in tracks:
                return False
            del tracks[track_key]
            if not tracks:
                del data[playlist_id]
            _save(data)
            return True
    except Exception as e:
        logger.log_error(f"Couldn't clear the failures of track {track_key}: {type(e).__name__}: {e}")
        return False


def prune(playlist_id: str, live_keys: Iterable[str]) -> None:
    """Drop the entries of tracks no longer in the live playlist (called
    at the end of a completed sync). Never raises."""
    live = set(live_keys)
    try:
        with _lock:
            data = _load()
            tracks = data.get(playlist_id)
            if not tracks:
                return
            stale = [k for k in tracks if k not in live]
            if not stale:
                return
            for k in stale:
                del tracks[k]
            if not tracks:
                del data[playlist_id]
            _save(data)
    except Exception as e:
        logger.log_error(f"Couldn't tidy {FAILURES_FILENAME}: {type(e).__name__}: {e}")


def track_key(track) -> Optional[str]:
    """The key a live track is stored under: the bare video ID for a
    YouTube-native track, the SoundCloud track ID for a SoundCloud song
    (v0.16.52), else the Spotify Track ID."""
    return (getattr(track, "soundcloud_id", None) or getattr(track, "video_id", None)
            or getattr(track, "spotify_id", None))


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """yt-dlp colours its error text when it thinks it's on a terminal
    (seen in old Sync Logs as "[0;31mERROR:[0m")."""
    return _ANSI.sub("", text or "").replace("[0;31m", "").replace("[0m", "")
