"""
Mud Music Manager - UI Backend Adapter (v0.16.3)

The only bridge between the GUI and the existing engine. It contains NO
sync, diff, download, tagging or config logic of its own: every task
below just calls the same functions main.py's terminal menu already
calls (library_sync.sync_playlist / scan_playlist /
check_playlist_integrity / fix_track, config_manager.*,
exceptions_manager.*, dependency_checker.*), on a background thread, and
turns their plain-text progress callbacks into Qt signals. All the
protected systems (design doc §3.3 diff, §3.4 matching, §3.8 crash
safeguards) are untouched.

Pause / Cancel (Sync Progress panel)
------------------------------------
library_sync.sync_playlist() is one long call, so the GUI hooks the
`on_progress` callback it already accepts. That callback fires right
before each track starts downloading, which is exactly the safe moment
to stop: the previous track has been fully downloaded, tagged and
committed, and the next one hasn't begun. So Pause blocks there, and
Cancel raises SyncCancelled there (unwinding before the removal step, so
a cancelled sync never deletes anything). Both therefore wait for the
current download to finish -- yt-dlp can't be paused or stopped safely
mid-download. A cancelled playlist does not write its Sync Log.txt entry
(that is written at the very end of library_sync's own sync).

Scans (the launch-time refresh and the Scan button) cancel the same way
since v0.16.14 ("Cancel Scan, Use Cache"), but a scan is read-only, so
it may stop at any progress message: the playlist being read keeps its
cached numbers and nothing is written to the cache for it.

Stopping one playlist (v0.16.36, Checklist item 37): ✕ on the playlist
being synced asks SyncControl to stop only that playlist. At the same
safe boundary it raises PlaylistStopped instead of SyncCancelled;
sync_task records that playlist as stopped (no removal step, no Sync Log
entry, as for Cancel) and goes on with the next one.
"""

import copy
import os
import re
import threading
import traceback
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QImage

import acquisition_engine
import app_update
import config_manager
import debug_console
import dependency_checker
import exceptions_manager
import library_sync
import logger
import metadata_tagger
import spotify_reader
import track_failures
import ui_settings


# ---------------------------------------------------------------------------
# Cancel / pause control
# ---------------------------------------------------------------------------

class SyncCancelled(Exception):
    """Raised at a safe track boundary when the user pressed Cancel.
    `partial` carries the per-playlist results gathered so far."""

    def __init__(self, partial=None):
        super().__init__("cancelled")
        self.partial = partial or []


class PlaylistStopped(Exception):
    """Raised at a safe track boundary when the user stopped only the
    playlist being synced (v0.16.36, Checklist item 37). sync_task catches
    it and carries on with the next playlist."""


class SyncControl(QObject):
    """Shared pause/cancel flags between the GUI and a worker thread.
    state: running | pausing | paused | cancelling

    v0.16.36: also "stop this playlist only". sync_task names the playlist
    it is on (begin_playlist); request_stop_playlist(key) only counts for
    that playlist, so a ✕ pressed just as one playlist ends never stops
    the next one."""

    stateChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._resume = threading.Event()
        self._resume.set()
        self.pause_requested = False
        self.cancel_requested = False
        self.state = "running"
        self.current_key: Optional[str] = None  # the playlist being synced
        self.stop_key: Optional[str] = None     # the playlist asked to stop

    def _set_state(self, state: str) -> None:
        self.state = state
        self.stateChanged.emit(state)

    def request_pause(self) -> None:
        if self.cancel_requested:
            return
        self.pause_requested = True
        self._resume.clear()
        self._set_state("pausing")

    def request_resume(self) -> None:
        self.pause_requested = False
        self._resume.set()
        if not self.cancel_requested:
            self._set_state("running")

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.pause_requested = False
        self._resume.set()
        self._set_state("cancelling")

    def begin_playlist(self, key: Optional[str]) -> None:
        """Worker thread: this playlist starts now. A stop asked for an
        earlier one is forgotten."""
        self.current_key = key
        if self.stop_key != key:
            self.stop_key = None

    def end_playlist(self) -> None:
        """Worker thread: the playlist finished (a stop asked for too late
        to take effect is dropped)."""
        self.stop_key = None
        self.current_key = None

    def request_stop_playlist(self, key: str) -> bool:
        """Stop only the playlist being synced, at the next safe point. A
        paused sync wakes up to stop it and stays paused for the next one.
        False if that playlist isn't the one being synced."""
        if self.cancel_requested or key is None or key != self.current_key:
            return False
        self.stop_key = key
        self._resume.set()
        return True

    def _take_stop(self) -> None:
        if self.stop_key is not None and self.stop_key == self.current_key:
            self.stop_key = None
            if self.pause_requested:
                self._resume.clear()  # still paused: the next playlist waits
            raise PlaylistStopped()

    def checkpoint(self) -> None:
        """Called from the worker thread at a safe boundary."""
        if self.cancel_requested:
            raise SyncCancelled()
        self._take_stop()
        if self.pause_requested:
            self._set_state("paused")
            self._resume.wait()
            if self.cancel_requested:
                raise SyncCancelled()
            self._take_stop()


# ---------------------------------------------------------------------------
# Data passed between worker threads and the GUI
# ---------------------------------------------------------------------------

@dataclass
class Progress:
    """What the Sync Progress panel displays."""
    playlist_index: int = 0
    playlist_total: int = 0
    playlist_name: str = ""
    track_index: int = 0
    track_total: int = 0
    text: str = ""
    # v0.17.6 (Checklist item 57): estimated seconds left in the whole Sync
    # (None: not known yet), and whether it is only a lower bound (a
    # playlist still to come has an unknown number of songs to download).
    eta_seconds: Optional[float] = None
    eta_open: bool = False

    @property
    def segments_value(self) -> float:
        """Overall progress measured in playlists (1.4 = playlist 1 done,
        playlist 2 40% done) -- drives the segmented bar."""
        done = max(0, self.playlist_index - 1)
        within = (self.track_index / self.track_total) if self.track_total else 0.0
        return done + min(1.0, within)


@dataclass
class PlaylistRunResult:
    """Outcome of one Sync / Scan / Integrity check for one playlist."""
    entry: dict
    kind: str  # sync | scan | integrity
    result: object = None  # SyncResult / ScanResult / IntegrityResult
    error: Optional[str] = None
    # v0.16.36 (Checklist item 37): the user stopped this playlist's sync
    # (✕); result is None, and this many tracks downloaded before it stopped
    stopped: bool = False
    stopped_downloaded: int = 0

    @property
    def name(self) -> str:
        return self.entry.get("folder_name", "?")


@dataclass
class LocalTrack:
    path: str
    identity: str
    key: str            # the key exceptions_manager uses for this track
    number: int
    title: str
    artist: str
    album: str
    seconds: float
    thumb: Optional[QImage] = None
    # v0.16.40 (Checklist item 47): the original file name if this song
    # was imported from the user's own file ("Own file: ..." tag).
    own_file: Optional[str] = None
    # v0.16.48 (Checklist item 41a): Disc # of an album song (0 if none).
    disc: int = 0


@dataclass
class ImportChoice:
    """One pending import on the Edit page (v0.16.40, Checklist item 47):
    the user's audio file for a song. For a song unavailable on Spotify
    (spacer) the title / artist (and optional album / year) are typed in,
    and spotify_id / position say which playlist slot it fills."""
    source_path: str
    spacer: bool = False
    spotify_id: str = ""
    position: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""


@dataclass
class EditData:
    tracks: List[LocalTrack] = field(default_factory=list)
    exceptions: Dict[str, str] = field(default_factory=dict)  # key -> youtube video id
    failures: Dict[str, dict] = field(default_factory=dict)   # key -> track_failures record (v0.16.27)


# ---------------------------------------------------------------------------
# Progress-message parsing (library_sync's on_progress strings)
# ---------------------------------------------------------------------------

_RE_TRACK_LINE = re.compile(r"^\[(\d+)/(\d+)\]\s+(.*)$")
_RE_DOWNLOADING = re.compile(r"^Downloading '(.*)'\.\.\.$")
_RE_FOUND = re.compile(r"^Found (\d+) (?:tracks|videos)")
_RE_TO_DOWNLOAD = re.compile(r"^(\d+) new tracks? to download\.$")  # v0.17.6


def parse_progress_message(msg: str) -> dict:
    """Classify one of library_sync's progress strings. Returns a dict
    with at least 'kind'."""
    match = _RE_TRACK_LINE.match(msg)
    if match:
        idx, total, rest = int(match.group(1)), int(match.group(2)), match.group(3)
        d = _RE_DOWNLOADING.match(rest)
        if d:
            return {"kind": "downloading", "index": idx, "total": total, "name": d.group(1)}
        if rest.startswith("FAILED"):
            return {"kind": "failed", "index": idx, "total": total, "reason": rest[6:].lstrip(": ")}
        if rest.startswith("Done"):
            return {"kind": "done", "index": idx, "total": total}
    if msg.startswith("Fetching live"):  # playlist, or album (v0.16.48)
        return {"kind": "fetching"}
    found = _RE_FOUND.match(msg)
    if found:
        return {"kind": "found", "total": int(found.group(1))}
    to_download = _RE_TO_DOWNLOAD.match(msg)
    if to_download:
        return {"kind": "to_download", "count": int(to_download.group(1))}
    if msg.startswith("Removed '"):
        return {"kind": "removing", "text": msg}
    if msg.startswith("Sync complete") or msg.startswith("Scan complete"):
        return {"kind": "complete", "text": msg}
    return {"kind": "other", "text": msg}


# ---------------------------------------------------------------------------
# Reading local files (no network -- works offline)
# ---------------------------------------------------------------------------

def _exception_key_for(identity: str, platform: str) -> str:
    """Exceptions are keyed by the Spotify track ID, or (YouTube-native)
    by the bare video ID -- the Comment tag holds 'yt:<videoId>' for the
    latter (and for YT Music, v0.16.51)."""
    if platform in config_manager.VIDEO_PLATFORMS and identity.startswith(metadata_tagger.YOUTUBE_IDENTITY_PREFIX):
        return identity[len(metadata_tagger.YOUTUBE_IDENTITY_PREFIX):]
    if platform == config_manager.PLATFORM_SOUNDCLOUD and \
            identity.startswith(metadata_tagger.SOUNDCLOUD_IDENTITY_PREFIX):  # v0.16.52
        return identity[len(metadata_tagger.SOUNDCLOUD_IDENTITY_PREFIX):]
    return identity


def read_local_tracks(folder, platform: str = config_manager.PLATFORM_SPOTIFY, with_thumbs: bool = False,
                      thumb_size: int = 96) -> List[LocalTrack]:
    """Every readable song file directly inside `folder` (any format the
    app downloads: .m4a, and since v0.16.45 .mp3 / .ogg / .flac / .wav,
    read through metadata_tagger.read_tags()), ordered by Track # (then
    title). Only the folder's direct children are looked at, same as
    library_sync's own scan, so the .mmm_staging subfolder is never
    included."""
    folder = Path(folder)
    tracks: List[LocalTrack] = []
    if not folder.exists():
        return tracks

    for path in metadata_tagger.audio_files(folder):
        tags = metadata_tagger.read_tags(path, with_cover=with_thumbs)
        if tags is None:
            logger.log_error(f"GUI: couldn't read tags from {path}")
            continue

        identity = tags.identity or ""
        thumb = None
        if with_thumbs and tags.cover:
            img = QImage.fromData(bytes(tags.cover))
            if not img.isNull():
                thumb = img.scaled(thumb_size, thumb_size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)

        tracks.append(LocalTrack(
            path=str(path),
            identity=identity,
            key=_exception_key_for(identity, platform) if identity else "",
            number=tags.track_number or 0,
            title=tags.title or path.stem,
            artist=tags.artist,
            album=tags.album,
            seconds=tags.length,
            thumb=thumb,
            own_file=metadata_tagger.own_file_name(tags.youtube_id),
            disc=tags.disc_number or 0,
        ))

    tracks.sort(key=lambda t: (t.number if t.number else 10 ** 6, t.title.lower()))
    return tracks


def compute_local_stats(entry: dict, folder) -> dict:
    tracks = read_local_tracks(folder, config_manager.get_playlist_platform(entry), with_thumbs=False)
    exceptions = exceptions_manager.list_exceptions(config_manager.get_playlist_source_id(entry))
    return {
        "tracks": len(tracks),
        "seconds": sum(t.seconds for t in tracks),
        "exceptions": len(exceptions),
    }


def missing_records(tracks, platform: str) -> List[dict]:
    """Plain-dict records for tracks that are live but not downloaded
    (or failed to download). Cached so the Edit page can still list them
    -- and let an Exception be attached to them -- while offline."""
    out = []
    for t in tracks:
        key = (getattr(t, "video_id", None) if platform in config_manager.VIDEO_PLATFORMS  # YT Music too
               else getattr(t, "soundcloud_id", None) if platform == config_manager.PLATFORM_SOUNDCLOUD
               else getattr(t, "spotify_id", None))
        if not key:
            continue
        out.append({
            "key": key,
            "number": int(getattr(t, "list_position", 0) or 0),
            "disc": int(getattr(t, "disc_number", 0) or 0),  # v0.16.48: album songs only
            "title": getattr(t, "name", "") or "",
            "artist": getattr(t, "primary_artist", "") or "",
            "album": getattr(t, "album_name", "") or "",
            "seconds": float(getattr(t, "duration_seconds", 0.0) or 0.0),
        })
    return out


def unavailable_records(items) -> List[dict]:
    """Plain-dict records of a playlist's items Spotify gives no details
    for (v0.16.39, Checklist items 45 / 46), cached for the Main Menu
    pill and the Edit page's spacer rows. Not pending: they can't be
    downloaded."""
    return [{
        "number": int(u.list_position),
        "key": u.spotify_id,
        "name": u.name,
        "reason": u.reason,
        "reason_text": u.reason_text,
    } for u in items]


def unavailable_text(n: int) -> str:
    """The neutral pill: "1 unavailable on Spotify" ("" for 0)."""
    return f"{n} unavailable on Spotify" if n else ""


def unavailable_tooltip(records: List[dict]) -> str:
    lines = [f"#{r.get('number', '?')}: {r.get('reason_text') or 'Spotify gives no details for it'}"
             for r in records]
    lines.append("Spotify lists these in the playlist but gives no song details, so they can't be "
                 "downloaded. They keep their Track # and aren't counted as pending.")
    return "\n".join(lines)


def count_pending(scan_result) -> int:
    return len(scan_result.missing) + len(scan_result.no_longer_live) + len(scan_result.needs_renumber)


# ---------------------------------------------------------------------------
# "No match" / "Unavailable" (v0.16.27, Checklist item 28)
# ---------------------------------------------------------------------------

def _red_counts(records) -> tuple:
    statuses = [track_failures.status(r) for r in records]
    return statuses.count(track_failures.STATUS_NO_MATCH), statuses.count(track_failures.STATUS_UNAVAILABLE)


def scan_attention(scan_result) -> dict:
    """What a Scan means for the playlist's status: tracks shown in red
    ("No match", "Unavailable") are not counted as pending."""
    no_match, unavailable = _red_counts(scan_result.known_failures.values())
    return {"pending": max(0, count_pending(scan_result) - no_match - unavailable),
            "no_match": no_match, "unavailable": unavailable}


def sync_attention(sync_result) -> dict:
    """Same as scan_attention(), after a Sync: what still failed."""
    no_match, unavailable = _red_counts(sync_result.failure_details)
    return {"pending": max(0, len(sync_result.failed) - no_match - unavailable),
            "no_match": no_match, "unavailable": unavailable}


def red_text(no_match: int, unavailable: int) -> str:
    """Text of the red status pill, e.g. "1 No match · 2 Unavailable"
    ("" when both are 0)."""
    parts = []
    if no_match:
        parts.append(f"{no_match} No match")
    if unavailable:
        parts.append(f"{unavailable} Unavailable")
    return " · ".join(parts)


def red_tooltip(no_match: int, unavailable: int) -> str:
    lines = []
    if no_match:
        lines.append(f"No match ({no_match}): the search worked, but nothing found was the right length. "
                     "Add an exception link on the Edit page.")
    if unavailable:
        lines.append(f"Unavailable ({unavailable}): age-restricted on YouTube, or failed on "
                     f"{track_failures.UNAVAILABLE_AFTER} or more syncs. "
                     "Still tried on every sync; the Edit page shows why.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

def _mirror_progress(text) -> None:
    """Copy a task's progress text into the Debug Window buffer. Runs on the
    emitting (worker) thread; the buffer is thread-safe and never raises."""
    debug_console.write_line(f"[task] {text}")


class TaskThread(QThread):
    """Runs fn(thread) off the GUI thread. The function reports through
    the signals below; its return value arrives via resultReady."""

    progress = Signal(str)            # raw library_sync-style text
    progressInfo = Signal(object)     # Progress
    entryStarted = Signal(object)     # playlist entry dict
    entryDone = Signal(object)        # PlaylistRunResult
    localStats = Signal(str, object)  # playlist_key, stats dict
    resultReady = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable, parent=None):
        super().__init__(parent)
        self._fn = fn
        # v0.16.3: every progress line also goes to the Debug Window's buffer.
        self.progress.connect(_mirror_progress, Qt.DirectConnection)

    def run(self):
        try:
            result = self._fn(self)
        except SyncCancelled as c:
            self.cancelled.emit(c.partial)
            return
        except Exception as e:  # never let a worker die silently
            logger.log_error(f"GUI background task failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        except BaseException as e:  # e.g. a SystemExit/KeyboardInterrupt escaping a library: must not take the app down
            logger.log_error(f"GUI background task aborted by {type(e).__name__}: {e}\n{traceback.format_exc()}")
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.resultReady.emit(result)


# ---------------------------------------------------------------------------
# Task functions (each runs inside a TaskThread)
# ---------------------------------------------------------------------------

class GuiCall(QObject):
    """Runs fn on the GUI thread for a worker thread and hands back its
    result (v0.16.50): the worker waits (Qt.BlockingQueuedConnection).
    Used to add a watched artist's new releases to playlists.json from the
    GUI thread, which owns the config, in the middle of a scan or sync.
    Create it on the GUI thread. Called from the GUI thread it just runs
    fn. An exception in fn is logged and the call returns None."""

    _call = Signal(object)

    def __init__(self, fn: Callable, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._call.connect(self._run, Qt.BlockingQueuedConnection)

    def _run(self, box: dict) -> None:
        try:
            box["result"] = self._fn(*box["args"])
        except Exception as e:
            logger.log_error(f"GUI: call from a background task failed: {type(e).__name__}: {e}\n"
                             f"{traceback.format_exc()}")

    def __call__(self, *args):
        box = {"args": args}
        if QThread.currentThread() is self.thread():
            self._run(box)
        else:
            self._call.emit(box)
        return box.get("result")


def _check_artists(thread: "TaskThread", artist_ids: List[str], apply_new: Optional[Callable],
                   snap: dict, control: Optional[SyncControl] = None,
                   push: Optional[Callable[[str], None]] = None) -> List[dict]:
    """v0.16.50 (Checklist item 41c): read each watched artist's
    discography (here, on the worker thread) and have the GUI thread add
    its new releases (apply_new(artist_id, ArtistInfo) -> new entries).
    Returns the new album entries, for the run to scan / sync too. An
    artist that can't be read is logged and skipped."""
    import artist_watch  # here, so a GUI without artists never needs it
    added: List[dict] = []
    if apply_new is None:
        return added
    for artist_id in artist_ids:
        if control is not None:
            control.checkpoint()
        artist = config_manager.find_artist(snap, artist_id) or {}
        text = f"Checking {artist.get('name') or 'an artist'} for new releases…"
        thread.progress.emit(text)
        if push is not None:
            push(text)
        try:
            info = artist_watch.read_artist(artist_id)
        except RuntimeError as e:
            logger.log_error(f"GUI: couldn't check artist {artist_id} for new releases: {e}")
            continue
        if control is not None:
            control.checkpoint()
        new = apply_new(artist_id, info) or []
        if new:
            thread.progress.emit(f"{len(new)} new release(s) of {info.name} added.")
        added.extend(copy.deepcopy(new))
    return added


def _snapshot(config: dict) -> dict:
    """The worker gets its own copy so the GUI can reorder/rename while
    a background scan is mid-flight without the two stepping on each
    other."""
    return copy.deepcopy(config)


def _folder_for(config: dict, entry: dict):
    try:
        return config_manager.get_playlist_folder(config, entry), None
    except ValueError as e:
        return None, str(e)


def refresh_task(config: dict, control: SyncControl, apply_new: Optional[Callable] = None) -> Callable:
    """Launch-time refresh (and the 'refresh everything' button): first
    the fast, offline local numbers for every playlist so the list fills
    in immediately, then a real Scan of each one against the live
    source, one at a time. Never blocks anything -- the GUI is already
    on screen. Stops between playlists if control.cancel_requested.

    v0.16.50 (Checklist item 41c): with apply_new (a GuiCall), every
    watched artist is checked for new releases before the scans; the new
    albums are added (on the GUI thread) and scanned with the rest."""
    snap = _snapshot(config)

    def fn(thread: TaskThread):
        import artist_watch
        entries = list(config_manager.list_playlists(snap))
        for entry in entries:
            if control.cancel_requested:
                return []
            folder, err = _folder_for(snap, entry)
            if folder is None:
                continue
            try:
                stats = compute_local_stats(entry, folder)
            except Exception as e:
                logger.log_error(f"GUI: local stats failed for '{entry['folder_name']}': {e}")
                continue
            key = ui_settings.playlist_key(entry)
            ui_settings.update_cache(key, **stats)
            thread.localStats.emit(key, stats)

        results = []
        try:
            entries.extend(_check_artists(thread, artist_watch.watched_artist_ids(snap), apply_new,
                                          snap, control))
            for entry in entries:
                if control.cancel_requested:
                    break
                thread.entryStarted.emit(copy.deepcopy(entry))
                results.append(_scan_one(thread, snap, entry, control))
        except SyncCancelled:
            _mark_scanned_if_any(results)
            raise SyncCancelled(results)
        _mark_scanned_if_any(results)
        return results

    return fn


def _mark_scanned_if_any(results: List[PlaylistRunResult]) -> None:
    if results and any(r.error is None for r in results):
        ui_settings.mark_scanned_now()


def _scan_one(thread: TaskThread, config: dict, entry: dict,
              control: Optional[SyncControl] = None) -> PlaylistRunResult:
    """Scan one playlist and cache its numbers. With a control, Cancel
    (v0.16.14, "Cancel Scan, Use Cache") raises SyncCancelled at the next
    progress message, or once the live read returns -- before anything is
    cached or reported, so the playlist keeps its cached numbers. A scan is
    read-only, so stopping it at any of these points is safe."""
    folder, err = _folder_for(config, entry)
    if folder is None:
        out = PlaylistRunResult(entry=entry, kind="scan", error=err)
        thread.entryDone.emit(out)
        return out
    key = ui_settings.playlist_key(entry)

    def on_progress(msg: str) -> None:
        if control is not None:
            control.checkpoint()
        thread.progress.emit(msg)

    try:
        res = library_sync.scan_playlist(entry, folder, on_progress=on_progress)
        if control is not None:
            control.checkpoint()
        ui_settings.update_cache(
            key, scanned_at=time.time(), live_total=res.total_live_tracks,
            missing=missing_records(res.missing, config_manager.get_playlist_platform(entry)),
            spotify_unavailable=unavailable_records(res.unavailable_on_spotify),
            **scan_attention(res),
        )
        out = PlaylistRunResult(entry=entry, kind="scan", result=res)
    except SyncCancelled:
        raise
    except RuntimeError as e:
        out = PlaylistRunResult(entry=entry, kind="scan", error=str(e))
    except Exception as e:
        logger.log_error(f"GUI: scan failed for '{entry['folder_name']}': {type(e).__name__}: {e}")
        out = PlaylistRunResult(entry=entry, kind="scan", error=f"{type(e).__name__}: {e}")
    thread.entryDone.emit(out)
    return out


def scan_task(config: dict, entries: List[dict], control: Optional[SyncControl] = None,
              apply_new: Optional[Callable] = None) -> Callable:
    """The Scan button. With a control, Cancel stops it the same way as
    the launch-time refresh (see _scan_one) and the thread emits
    `cancelled` with the playlists finished so far.

    v0.16.50 (Checklist item 41c): with apply_new, the watched artists
    whose albums are being scanned are checked for new releases first;
    new albums are added and scanned too."""
    snap = _snapshot(config)
    entries = list(entries)

    def fn(thread: TaskThread):
        import artist_watch
        results = []
        try:
            entries.extend(_check_artists(thread, artist_watch.watched_artist_ids(snap, entries),
                                          apply_new, snap, control))
            for entry in entries:
                if control is not None:
                    control.checkpoint()
                thread.entryStarted.emit(copy.deepcopy(entry))
                results.append(_scan_one(thread, snap, entry, control))
        except SyncCancelled:
            _mark_scanned_if_any(results)
            raise SyncCancelled(results)
        _mark_scanned_if_any(results)
        return results

    return fn


def integrity_task(config: dict, entries: List[dict]) -> Callable:
    snap = _snapshot(config)

    def fn(thread: TaskThread):
        results = []
        for entry in entries:
            thread.entryStarted.emit(copy.deepcopy(entry))
            folder, err = _folder_for(snap, entry)
            if folder is None:
                out = PlaylistRunResult(entry=entry, kind="integrity", error=err)
            else:
                try:
                    res = library_sync.check_playlist_integrity(folder)
                    out = PlaylistRunResult(entry=entry, kind="integrity", result=res)
                except Exception as e:
                    logger.log_error(f"GUI: integrity check failed for '{entry['folder_name']}': {e}")
                    out = PlaylistRunResult(entry=entry, kind="integrity", error=f"{type(e).__name__}: {e}")
            thread.entryDone.emit(out)
            results.append(out)
        return results

    return fn


class SyncEstimate:
    """The Sync Progress panel's ETA (v0.17.6, Checklist item 57).

    Time left = the downloads still to come x the average time per download
              + the playlists still to come x the average time a playlist
                spends on anything but downloads (reading it live, comparing,
                renumbering, removing).

    Both averages are this Sync's own, measured as it goes, blended with
    the averages of earlier Syncs (ui_settings "sync_timing"), which count
    as at most HISTORY_WEIGHT samples: so there is an estimate before
    anything has been timed, and this Sync's own speed soon takes over.
    How many songs a waiting playlist will download comes from its cached
    Scan (its missing songs); for the playlist being synced, from
    library_sync's "N new tracks to download" once it has been read. A
    playlist with no cached Scan adds an unknown number of downloads, so
    the estimate is then a lower bound (`open`). Time paused is left out
    of every measure. Worker thread only; no Qt."""

    SETTINGS_KEY = "sync_timing"
    HISTORY_WEIGHT = 5   # earlier Syncs count as this many samples, at most
    HISTORY_CAP = 50     # the stored averages follow about the last 50 samples

    def __init__(self, pending):
        self.pending: List[Optional[int]] = list(pending)  # per playlist; None: not known
        hist = ui_settings.get(self.SETTINGS_KEY)
        self._hist = dict(hist) if isinstance(hist, dict) else {}
        self._run = {"download": [0.0, 0], "overhead": [0.0, 0]}  # this Sync: seconds, samples
        self._index = -1             # the playlist being synced (0-based), or the last one
        self._pl_start = None        # monotonic start of that playlist; None between playlists
        self._pl_paused = 0.0
        self._pl_download = 0.0
        self._read_secs = None       # how long its live read took
        self._left: Optional[int] = None  # its downloads not finished yet, once known
        self._dl_start = None

    @staticmethod
    def cached_downloads(entry: dict) -> Optional[int]:
        """How many songs this playlist's last Scan / Sync found missing."""
        missing = ui_settings.get_cache(ui_settings.playlist_key(entry)).get("missing")
        return len(missing) if isinstance(missing, list) else None

    def _history(self, kind: str):
        try:
            mean = float(self._hist.get(f"{kind}_secs"))
            n = int(self._hist.get(f"{kind}_n", 0))
        except (TypeError, ValueError):
            return None, 0
        return (mean, n) if mean >= 0 and n > 0 else (None, 0)

    def _mean(self, kind: str) -> Optional[float]:
        total, n = self._run[kind]
        h_mean, h_n = self._history(kind)
        weight = min(h_n, self.HISTORY_WEIGHT)
        if n + weight == 0:
            return None
        return (total + (h_mean or 0.0) * weight) / (n + weight)

    def _active(self) -> float:
        return time.monotonic() - self._pl_start - self._pl_paused

    # ---- what the sync is doing ------------------------------------------
    def add_playlists(self, pending) -> None:
        self.pending.extend(pending)

    def begin_playlist(self, index: int) -> None:
        self._index = index
        self._pl_start = time.monotonic()
        self._pl_paused = self._pl_download = 0.0
        self._read_secs = self._left = self._dl_start = None

    def paused(self, seconds: float) -> None:
        if self._pl_start is not None:
            self._pl_paused += seconds

    def downloads_known(self, count: int) -> None:
        self._left = count
        if self._pl_start is not None and self._read_secs is None:
            self._read_secs = self._active()

    def download_started(self) -> None:
        self._dl_start = time.monotonic()

    def download_finished(self) -> None:
        if self._dl_start is None:
            return
        seconds = time.monotonic() - self._dl_start
        self._dl_start = None
        self._run["download"][0] += seconds
        self._run["download"][1] += 1
        self._pl_download += seconds
        if self._left:
            self._left -= 1

    def end_playlist(self, finished: bool) -> None:
        """finished: it ran to the end, so its time is a fair sample (not a
        stopped, cancelled or failed one)."""
        if finished and self._pl_start is not None:
            self._run["overhead"][0] += max(0.0, self._active() - self._pl_download)
            self._run["overhead"][1] += 1
        self._pl_start = self._dl_start = None

    # ---- the estimate ----------------------------------------------------
    def estimate(self):
        """(seconds left, open), or (None, False) while nothing can be said."""
        dl = self._mean("download")
        overhead = self._mean("overhead")
        if overhead is None:
            overhead = self._read_secs  # first Sync ever: this playlist's own read
        now = time.monotonic()
        open_ = False
        downloads = 0
        seconds = 0.0
        in_playlist = self._pl_start is not None
        if in_playlist:
            if self._left is None:  # still being read
                n = self.pending[self._index] if 0 <= self._index < len(self.pending) else None
                open_ = open_ or n is None
                downloads += n or 0
            else:
                downloads += self._left
        waiting = self.pending[self._index + 1:]
        for n in waiting:
            open_ = open_ or n is None
            downloads += n or 0
        if dl is None and (downloads or open_):
            return None, False
        dl = dl or 0.0
        if overhead is None:
            overhead, open_ = 0.0, True
        seconds += downloads * dl + len(waiting) * overhead
        if in_playlist:
            current = (now - self._dl_start) if self._dl_start is not None else 0.0
            seconds -= min(dl, current)  # the download under way is partly done
            spent = self._active() - self._pl_download - current
            seconds += max(0.0, overhead - spent)
        return max(0.0, seconds), open_

    def save(self) -> None:
        """Fold this Sync's timings into the stored averages."""
        hist = dict(self._hist)
        changed = False
        for kind, (total, n) in self._run.items():
            if not n:
                continue
            h_mean, h_n = self._history(kind)
            h_n = min(h_n, self.HISTORY_CAP)
            new_n = h_n + n
            hist[f"{kind}_secs"] = round(((h_mean or 0.0) * h_n + total) / new_n, 2)
            hist[f"{kind}_n"] = min(new_n, self.HISTORY_CAP)
            changed = True
        if changed:
            try:
                ui_settings.set_value(self.SETTINGS_KEY, hist)
            except Exception as e:
                logger.log_error(f"GUI: couldn't save the Sync timings: {e}")


def sync_task(config: dict, entries: List[dict], control: SyncControl,
              apply_new: Optional[Callable] = None) -> Callable:
    """Sync the chosen playlists in order, exactly like the terminal
    menu's Sync does (library_sync.sync_playlist per playlist), but with
    pause/cancel at safe track boundaries and progress as signals.

    v0.16.36 (Checklist item 37): control.request_stop_playlist() stops
    only the playlist being synced; it is reported as a stopped
    PlaylistRunResult and the next playlist starts.

    v0.16.50 (Checklist item 41c): with apply_new, the watched artists
    whose albums are being synced are checked for new releases first; new
    albums are added (apply_new also puts them in the GUI's queue list)
    and synced at the end of this run -- "added and downloaded
    automatically" (Michael's choice).

    v0.17.6 (Checklist item 57): each progress update carries the run's
    ETA (SyncEstimate); the timings are saved when the run ends, even by
    Cancel, to seed the next Sync's first estimate."""
    snap = _snapshot(config)
    entries = list(entries)

    def fn(thread: TaskThread):
        import artist_watch
        results: List[PlaylistRunResult] = []
        prog = Progress(playlist_total=len(entries))
        est = SyncEstimate(SyncEstimate.cached_downloads(e) for e in entries)

        def push(text=None):
            if text is not None:
                prog.text = text
            prog.eta_seconds, prog.eta_open = est.estimate()
            thread.progressInfo.emit(replace(prog))

        def checkpoint():
            """control.checkpoint(), the time it waits (paused) left out of the ETA."""
            t = time.monotonic()
            try:
                control.checkpoint()
            finally:
                est.paused(time.monotonic() - t)

        def stop_run():
            est.save()
            return SyncCancelled(results)

        try:
            new_entries = _check_artists(thread, artist_watch.watched_artist_ids(snap, entries),
                                         apply_new, snap, control, push)
            entries.extend(new_entries)
            est.add_playlists(None for _ in new_entries)  # new albums: never scanned
        except SyncCancelled:
            raise stop_run()
        except PlaylistStopped:
            pass  # nothing is being synced yet
        prog.playlist_total = len(entries)

        for i, entry in enumerate(entries, start=1):
            key = ui_settings.playlist_key(entry)
            control.begin_playlist(key)
            name = entry["folder_name"]
            downloaded = [0]  # tracks downloaded so far in this playlist
            try:
                checkpoint()
            except SyncCancelled:
                raise stop_run()
            except PlaylistStopped:  # stopped while paused before it began
                out = PlaylistRunResult(entry=entry, kind="sync", stopped=True)
                thread.entryDone.emit(out)
                results.append(out)
                est.begin_playlist(i - 1)
                est.end_playlist(False)
                control.end_playlist()
                continue

            prog.playlist_index, prog.playlist_name = i, name
            prog.track_index = prog.track_total = 0
            est.begin_playlist(i - 1)
            thread.entryStarted.emit(copy.deepcopy(entry))
            push(f"Reading “{name}”…")

            folder, err = _folder_for(snap, entry)
            if folder is None:
                out = PlaylistRunResult(entry=entry, kind="sync", error=err)
                thread.entryDone.emit(out)
                results.append(out)
                est.end_playlist(False)
                control.end_playlist()
                continue

            def on_progress(msg: str, _prog=prog, _downloaded=downloaded):
                info = parse_progress_message(msg)
                kind = info["kind"]
                if kind == "downloading":
                    # The safe boundary -- the previous track is fully done.
                    checkpoint()
                    est.download_started()
                    _prog.track_index, _prog.track_total = info["index"], info["total"]
                    push(f"Downloading “{info['name']}”")
                elif kind in ("done", "failed"):
                    if kind == "done":
                        _downloaded[0] += 1
                    est.download_finished()
                    _prog.track_index, _prog.track_total = info["index"], info["total"]
                    push()
                elif kind == "found":
                    _prog.track_total = info["total"]
                    push(f"Comparing {info['total']} tracks…")
                elif kind == "to_download":  # v0.17.6
                    est.downloads_known(info["count"])
                    push()
                elif kind == "removing":
                    push("Removing tracks no longer in the playlist…")
                thread.progress.emit(msg)

            try:
                result = library_sync.sync_playlist(entry, folder, on_progress=on_progress)
                out = PlaylistRunResult(entry=entry, kind="sync", result=result)
                try:
                    stats = compute_local_stats(entry, folder)
                    stats.update(
                        scanned_at=time.time(),
                        missing=missing_records(
                            [r.track for r in result.downloaded if r.file_path is None],
                            config_manager.get_playlist_platform(entry)),
                        spotify_unavailable=unavailable_records(result.unavailable_on_spotify),
                        **sync_attention(result),
                    )
                    key = ui_settings.playlist_key(entry)
                    ui_settings.update_cache(key, **stats)
                    thread.localStats.emit(key, stats)
                except Exception as e:
                    logger.log_error(f"GUI: post-sync stats failed for '{name}': {e}")
            except SyncCancelled:
                raise stop_run()
            except PlaylistStopped:
                # Like Cancel, but only this playlist: it unwound before its
                # removal step and Sync Log entry. Its local numbers are
                # refreshed (tracks downloaded before the stop count); its
                # pending count stays as it was until the next scan.
                logger.log_event(f"GUI: sync of '{name}' stopped by the user after "
                                   f"{downloaded[0]} download(s); going on with the next playlist.")
                out = PlaylistRunResult(entry=entry, kind="sync", stopped=True,
                                        stopped_downloaded=downloaded[0])
                try:
                    stats = compute_local_stats(entry, folder)
                    ui_settings.update_cache(key, **stats)
                    thread.localStats.emit(key, stats)
                except Exception as e:
                    logger.log_error(f"GUI: local stats after a stopped sync failed for '{name}': {e}")
            except RuntimeError as e:
                out = PlaylistRunResult(entry=entry, kind="sync", error=str(e))
            except Exception as e:
                logger.log_error(f"GUI: sync failed for '{name}': {type(e).__name__}: {e}")
                out = PlaylistRunResult(entry=entry, kind="sync", error=f"{type(e).__name__}: {e}")
            est.end_playlist(out.result is not None)
            control.end_playlist()
            thread.entryDone.emit(out)
            results.append(out)

        est.save()
        if any(r.error is None for r in results):
            ui_settings.mark_scanned_now()
        return results

    return fn


def edit_data_task(entry: dict, folder) -> Callable:
    """Everything the Edit page lists, read from the local files only
    (no network), so it opens instantly and works offline."""
    platform = config_manager.get_playlist_platform(entry)
    source_id = config_manager.get_playlist_source_id(entry)

    def fn(thread: TaskThread):
        tracks = read_local_tracks(folder, platform, with_thumbs=True)
        return EditData(tracks=tracks, exceptions=dict(exceptions_manager.list_exceptions(source_id)),
                        failures=track_failures.get_playlist(source_id))

    return fn


def save_exceptions_task(entry: dict, folder, changes: Dict[str, Optional[str]],
                         imports: Optional[Dict[str, "ImportChoice"]] = None) -> Callable:
    """Apply the Edit page's pending exception changes and (v0.16.40,
    Checklist item 47) imports of the user's own audio files.
    changes: {track_key: youtube link/ID, or None to remove the exception}.
    imports: {row key: ImportChoice}, applied with library_sync.import_track().

    Removing mirrors the terminal menu's Exceptions -> Remove (it does
    not touch the file). Setting/changing mirrors Exceptions -> Fix: the
    live playlist is fetched once to find the track, then
    library_sync.fix_track() saves the exception, removes the old file
    and re-downloads it from the new link. Returns [(key, ok, message)]."""
    platform = config_manager.get_playlist_platform(entry)
    source_id = config_manager.get_playlist_source_id(entry)

    def fn(thread: TaskThread):
        outcomes = []
        sets = {k: v for k, v in changes.items() if v}
        removals = [k for k, v in changes.items() if not v]

        for key in removals:
            thread.progress.emit("Removing exception…")
            exceptions_manager.remove_exception(source_id, key)
            outcomes.append((key, True, "Exception removed (the file itself was left alone)."))

        to_import = imports or {}
        if sets or to_import:
            thread.progress.emit("Reading the live playlist…")
            unavailable = []  # v0.16.39: still counted in Track #'s "of total"
            # v0.16.40: only the songs being fixed / imported need Spotify's
            # details (album, year, cover); the rest of the playlist doesn't.
            wanted = set(sets) | {c.spotify_id if c.spacer else k for k, c in to_import.items()}
            try:
                if config_manager.is_soundcloud(entry):  # v0.16.52
                    live = acquisition_engine.get_soundcloud_tracks(entry)
                elif config_manager.uses_video_ids(entry):
                    # YouTube: the video list only, as before; YT Music
                    # (v0.16.51): full details for the songs being fixed.
                    live = acquisition_engine.get_video_tracks(entry, details_only_for=wanted)
                else:
                    # Spotify or (v0.16.53) Apple Music
                    live = acquisition_engine.get_catalog_tracks(entry, unavailable=unavailable,
                                                                 details_only_for=wanted)
            except Exception as e:
                logger.log_error(f"GUI: couldn't read live playlist to apply exceptions: {e}")
                for key in list(sets) + list(to_import):
                    outcomes.append((key, False, f"Couldn't read the live playlist: {e}"))
                return outcomes

            by_key = {}
            for t in live:
                by_key[track_failures.track_key(t)] = t  # SoundCloud songs too (v0.16.52)
            total = library_sync.position_total(live, unavailable)

            for key, link in sets.items():
                track = by_key.get(key)
                if track is None:
                    outcomes.append((key, False, "This track is no longer in the live playlist."))
                    continue
                thread.progress.emit(f"Fixing “{track.name}” — this may take a moment…")
                try:
                    res = library_sync.fix_track(track, entry, folder, link, total_tracks=total)
                except ValueError as e:
                    outcomes.append((key, False, str(e)))
                    continue
                if res.file_path:
                    outcomes.append((key, True, "Re-downloaded and tagged."))
                else:
                    # v0.16.38: fix_track() no longer deletes the song's
                    # existing file before a download that fails.
                    outcomes.append((key, False, f"Exception saved, but the download failed "
                                                 f"({track_failures.label(res.failure_kind)}): {res.failure_reason} "
                                                 f"Any file the song already had was kept."))

            imported = []
            for key, choice in to_import.items():
                track = _import_target(choice, key, by_key, unavailable)
                if track is None:
                    outcomes.append((key, False, "This song is no longer in the live playlist, so the file "
                                                 "wasn't imported."))
                    continue
                name = os.path.basename(choice.source_path)
                thread.progress.emit(f"Importing “{name}” as “{track.name}”…")
                res = library_sync.import_track(track, entry, folder, choice.source_path, total_tracks=total)
                if res.file_path:
                    outcomes.append((key, True, "Imported and tagged."))
                    imported.append(choice.spotify_id if choice.spacer else key)
                else:
                    outcomes.append((key, False, f"The file wasn't imported: {res.failure_reason}. "
                                                 f"Nothing else was changed."))
            if imported:
                _forget_cached_missing(entry, imported)
        return outcomes

    return fn


def _import_target(choice: "ImportChoice", key: str, by_key: dict, unavailable: list):
    """The track an import fills (v0.16.40): the live track for an
    ordinary song; for a song unavailable on Spotify, a Track built from
    the typed details at its current position (or the live track, if
    Spotify has since made it available). None if it left the playlist."""
    if not choice.spacer:
        return by_key.get(key)
    if choice.spotify_id in by_key:
        return by_key[choice.spotify_id]
    slot = next((u for u in unavailable if u.spotify_id == choice.spotify_id), None)
    if slot is None:
        return None
    return spotify_reader.Track(
        spotify_id=choice.spotify_id, name=choice.title.strip(), artists=[choice.artist.strip()],
        album_name=choice.album.strip() or None, year=choice.year.strip() or None,
        list_position=slot.list_position,
    )


def _forget_cached_missing(entry: dict, keys: List[str]) -> None:
    """After an import (v0.16.40) the song has a file: drop it from the
    cached not-downloaded and unavailable-on-Spotify lists, so the Edit
    page and the Main Menu pill don't show it as missing until the next
    scan."""
    key = ui_settings.playlist_key(entry)
    cache = ui_settings.get_cache(key)
    done = set(keys)
    ui_settings.update_cache(
        key,
        missing=[m for m in cache.get("missing") or [] if m.get("key") not in done],
        spotify_unavailable=[u for u in cache.get("spotify_unavailable") or [] if u.get("key") not in done],
    )


def check_updates_task() -> Callable:
    """The libraries (dependency_checker) and, since v0.19.0 (Checklist item
    60b), whether a newer Mud Music Manager is published (app_update):
    {"libraries": [PackageStatus], "app": AppUpdateStatus}."""
    def fn(thread: TaskThread):
        return {"libraries": dependency_checker.check_all(), "app": app_update.check_latest()}
    return fn


def download_update_task(status, cancel: dict) -> Callable:
    """Update now (v0.21.0, Checklist item 60e): download and check the
    release's installer (app_update.download_installer). Progress goes out as
    'done/total' bytes, at most once per percent; cancel["stop"] = True stops
    it. Returns the installer's path; an app_update.UpdateError ends up in
    the thread's failed signal with its message."""
    def fn(thread: TaskThread):
        last = {"pct": -1}

        def progress(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else done // 1_048_576
            if pct != last["pct"]:
                last["pct"] = pct
                thread.progress.emit(f"{done}/{total}")

        return app_update.download_installer(status, on_progress=progress,
                                             should_cancel=lambda: bool(cancel.get("stop")))
    return fn


def install_updates_task(statuses) -> Callable:
    def fn(thread: TaskThread):
        lines = dependency_checker.update_outdated(statuses)
        return {"lines": lines, "statuses": dependency_checker.check_all()}
    return fn


def change_library_task(config: dict, new_path: str, move_files: bool) -> Callable:
    """Runs config_manager.change_library_path() on the live config
    dict (it saves as it goes, with its own crash safeguards). The GUI
    shows a modal busy dialog meanwhile, so nothing else touches the
    config concurrently."""
    def fn(thread: TaskThread):
        resolved = config_manager.change_library_path(
            config, new_path, move_files=move_files, on_progress=thread.progress.emit
        )
        return {"resolved": resolved, "applied": config_manager.get_library_path(config) == resolved}
    return fn


# ---------------------------------------------------------------------------
# Thread bookkeeping
# ---------------------------------------------------------------------------

class _FinishWatcher(QObject):
    """Lives in the GUI thread; turns QThread.finished into a GUI-thread
    call on the Backend."""

    def __init__(self, backend, thread):
        super().__init__(backend)
        self._backend = backend
        self._thread = thread

    def on_finished(self):
        self._backend._on_finished(self._thread)
        self.deleteLater()


class Backend(QObject):
    """Owns the worker threads: keeps them alive until they finish,
    allows only one 'heavy' task (sync / integrity / save / path change)
    at a time, and lets a user action politely wait for the background
    refresh to stop first."""

    busyChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._threads = set()
        self._heavy: Optional[TaskThread] = None
        self.refresh_thread: Optional[TaskThread] = None
        self.refresh_control: Optional[SyncControl] = None
        self._after_refresh: List[Callable[[], None]] = []

    @property
    def heavy_running(self) -> bool:
        return self._heavy is not None

    @property
    def any_running(self) -> bool:
        return bool(self._threads)

    def _track(self, thread: TaskThread) -> None:
        self._threads.add(thread)
        # A bound method of a GUI-thread QObject => the call is queued onto
        # the GUI thread (a lambda would run on the worker thread).
        watcher = _FinishWatcher(self, thread)
        thread.finished.connect(watcher.on_finished)

    def _on_finished(self, thread: TaskThread) -> None:
        self._threads.discard(thread)
        if thread is self._heavy:
            self._heavy = None
            self.busyChanged.emit(False)
        was_refresh = thread is self.refresh_thread
        if was_refresh:
            self.refresh_thread = None
            self.refresh_control = None
        thread.deleteLater()
        if was_refresh:
            callbacks, self._after_refresh = self._after_refresh, []
            for cb in callbacks:
                cb()

    def run(self, fn: Callable, heavy: bool = True) -> TaskThread:
        thread = TaskThread(fn)
        self._track(thread)
        if heavy:
            self._heavy = thread
            self.busyChanged.emit(True)
        thread.start()
        return thread

    def start_refresh(self, config: dict, apply_new: Optional[Callable] = None) -> Optional[TaskThread]:
        """Background launch-time refresh. No-op if one is already running.
        apply_new (v0.16.50): see refresh_task()."""
        if self.refresh_thread is not None:
            return None
        control = SyncControl()
        self.refresh_control = control
        thread = TaskThread(refresh_task(config, control, apply_new))
        self.refresh_thread = thread
        self._track(thread)
        thread.start()
        return thread

    def cancel_refresh(self) -> bool:
        """"Cancel Scan, Use Cache" on the launch-time refresh (v0.16.14).
        True if a refresh was running and has been asked to stop."""
        if self.refresh_thread is None or self.refresh_control is None:
            return False
        self.refresh_control.request_cancel()
        return True

    def when_refresh_stopped(self, callback: Callable[[], None]) -> None:
        """Run callback now if no background refresh is running,
        otherwise ask it to stop at its next playlist boundary and run
        callback (on the GUI thread) once it has."""
        if self.refresh_thread is None:
            callback()
            return
        self._after_refresh.append(callback)
        if self.refresh_control is not None:
            self.refresh_control.request_cancel()

    def shutdown(self, wait_ms: int = 4000) -> None:
        """Best-effort on app exit: ask everything to stop, then wait a
        moment for the threads to finish."""
        if self.refresh_control:
            self.refresh_control.request_cancel()
        for t in list(self._threads):
            t.wait(wait_ms)
