"""
Mud Music Manager - Library Sync / Diff Engine

Compares a playlist's live track list against what's already
downloaded in its local folder, using the identity string burned into
each file's Comment tag as the key (design doc Section 3.3) -- no
separate tracking database, files are self-describing. That identity
string is the bare Spotify Track ID for a Spotify-native playlist, or
"yt:<videoId>" for a YouTube-native one (Checklist item 6, v0.15.0) --
the diff itself treats it as an opaque string and is identical for
both platforms; only how the live track list is fetched, and how a
missing track is acquired, differs per platform.

For each tracked playlist:
  - An ID in the live playlist but not locally -> download it
  - An ID locally but not in the live playlist -> remove the file
  - An ID in both -> already have it; skip re-download, but
    update its Track # if its position in the playlist changed

sync_playlist() / scan_playlist() take the playlist's whole config
entry (not just a bare ID string, as they did before v0.15.0) and
dispatch on its `platform` field to a Spotify-specific or
YouTube-specific implementation. The two implementations are
deliberately near-duplicates of each other rather than one shared
generic loop -- in keeping with this project's existing preference
for independently-testable modules over clever shared abstractions
(see config_manager/exceptions_manager's duplicated
_atomic_write_json()) -- so a change to one platform's path can never
accidentally alter the other's.

Alongside the real (mutating) sync above, this module also exposes a
read-only "Scan" (dry-run) version -- see scan_playlist() /
scan_all_tracked_playlists() -- which walks the exact same diff logic
but never downloads, deletes, or tags anything. Checklist item 2.

Checklist item 3 (crash / interruption failsafes) added a second,
separate read-only inspection tool -- check_playlist_integrity() /
check_all_tracked_playlists_integrity() -- for file-level integrity
rather than the live-vs-local diff: leftover files from an interrupted
download or tagging step (staged downloads, per acquisition_engine.py,
and staged renumber copies, per metadata_tagger.py), and files whose
tags can't be read at all or are missing the identity tag entirely.
Cleanup of anything it finds is a deliberately separate, explicit call
(clean_orphaned_temp_files()) -- inspecting never has a side effect on
its own, matching the same "inspect without acting" principle as Scan.
This tool only checks that a Comment tag exists, not what's in it, so
it needed no changes for YouTube-native playlists.

On the removal loop below (the other item-3 "highest risk operation"
named in the Checklist): each file removal is already a single,
independent, atomic OS-level unlink, logged individually, and downloads
always run to completion before any removals are attempted for a given
sync -- so a crash mid-removal-loop simply leaves the remaining
no-longer-live files to be caught (and safely removed) by the next
sync, with no partial/corrupted state possible. This was reviewed as
part of item 3 and found to already be crash-safe by construction,
rather than needing a structural change.

Failure tracking (v0.16.27, Checklist item 28): after each download
attempt a Sync records the failure (its kind, reason and how many syncs
it has failed on) in track_failures.json, or clears it when the track
downloads; a completed sync drops the entries of tracks no longer live.
A track whose acquisition raises an unexpected error is recorded as an
"App error" and the sync carries on with the next track (it used to stop
the whole playlist). Scan only reads those records (ScanResult.
known_failures), so the GUI can show "No match" / "Unavailable".

Unavailable on Spotify (v0.16.39, Checklist items 45 / 46): a Spotify
Sync / Scan also returns the playlist's items Spotify gives no details
for (`unavailable_on_spotify`, spotify_reader.UnavailableItem). They are
not live tracks for the diff: nothing is downloaded, removed or counted
as pending for them. The GUI shows them; the result only carries them.

Own files (v0.16.40, Checklist item 47): import_track() puts the user's
own audio file in the folder as a song (acquisition_engine.
import_audio_file()), like fix_track() does with a link. A Sync never
re-downloads it: it is just a file with the track's identity. An item
unavailable on Spotify whose file was imported counts as live
(_keep_imported_unavailable()): it is renumbered like any other and
never removed, and it leaves `unavailable_on_spotify`.

Other file formats (v0.16.45, Checklist item 39): a folder's songs can
be .m4a, .mp3, .ogg, .flac or .wav (Settings -> Download quality), and a
folder can hold several (files keep their format when the setting
changes). Every place below that read `folder.glob("*.m4a")` now uses
metadata_tagger.audio_files(), and tags are read with metadata_tagger,
so the diff itself is unchanged: it still compares the Comment
identities, whatever the file's format.

Albums (v0.16.48, Checklist item 41a): a Spotify album entry
(config_manager.is_album()) is synced, scanned, fixed and imported by
the same Spotify code; only the live read differs (acquisition_engine.
get_playlist_tracks(..., album=True), album order). An album Track's
list_position is its track number on its disc, so Track # and the
renumber check use it as they use a playlist position, and its "of
total" is the track count of its disc (_track_total()). The diff and its
actions are unchanged.

YT Music (v0.16.51, Checklist item 42a): a YT Music playlist or album
entry (config_manager.uses_video_ids()) goes through the YouTube code
path -- its songs are YouTube videos, "yt:<videoId>" identities, with
exceptions keyed by the bare video ID -- and only the live read differs
(acquisition_engine.get_video_tracks(), ytmusic_reader). A YT Music album
track carries its track number, Disc # and the album's track count, used
as for a Spotify album (_track_total()). The diff and its actions are
unchanged.

SoundCloud (v0.16.52, Checklist item 42b): a SoundCloud set goes through
the same known-songs path as YouTube / YT Music (_sync_youtube_playlist()),
with "sc:<track id>" identities and the SoundCloud track ID as the
exceptions / failures key (_song_identity() / _song_key()); its songs are
read by soundcloud_reader and downloaded by acquire_soundcloud_track()
(from SoundCloud, or from YouTube when SoundCloud only has a preview).
The diff and its actions are unchanged.

Apple Music (v0.16.53, Checklist item 42c): an Apple Music entry takes
the Spotify path (_sync_spotify_playlist(), the tiered YouTube search) --
its songs are Tracks whose spotify_id is "am:<song id>", read by
acquisition_engine.get_catalog_tracks(). The diff and its actions are
unchanged.
"""

import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import acquisition_engine
import metadata_tagger
import config_manager
import exceptions_manager
import logger
import spotify_reader
import track_failures

DOWNLOAD_LOG_FILENAME = "Sync Log.txt"
# v0.17.6 (Checklist item 57): the Sync Log's first line names the
# playlist's source link, once.
LOG_SOURCE_PREFIX = "Source: "

# Reason recorded in SyncResult.failed when an already-downloaded track's
# Track # couldn't be updated (not a download failure).
TRACK_NUMBER_FAILURE = "failed to update its track number"

# Reuse the exact same staging-subfolder name and retag-temp suffix the
# Acquisition Engine / Metadata Tagger use, so the integrity check below
# recognizes leftovers from an interrupted operation by the same names
# those modules actually create them with.
STAGING_SUBDIR_NAME = acquisition_engine.STAGING_SUBDIR_NAME
RETAG_TEMP_SUFFIX = metadata_tagger.RETAG_TEMP_SUFFIX


@dataclass
class SyncResult:
    playlist_folder: str
    downloaded: List[acquisition_engine.AcquisitionResult] = field(default_factory=list)
    renumbered: List[str] = field(default_factory=list)  # track names
    removed: List[str] = field(default_factory=list)  # filenames
    unchanged_count: int = 0
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (name, specific reason)
    # v0.16.27: one track_failures record per failed download (plus "key"
    # and "name"), in the order they failed.
    failure_details: List[dict] = field(default_factory=list)
    # v0.16.39: items Spotify gives no details for (spotify_reader.
    # UnavailableItem); always empty for a YouTube playlist.
    unavailable_on_spotify: List[object] = field(default_factory=list)
    # v0.17.6 (Checklist item 57): how long the sync took, without time
    # spent paused, and how much of it went on downloads (_SyncClock).
    elapsed_seconds: float = 0.0
    download_seconds: float = 0.0

    @property
    def total_live_tracks(self) -> int:
        return len(self.downloaded) + self.unchanged_count + len(self.renumbered) + len(self.failed)


@dataclass
class ScanResult:
    """
    Read-only counterpart to SyncResult -- reports what a real sync
    WOULD do to this playlist folder, without anything having actually
    happened yet.
    """
    playlist_folder: str
    missing: List[object] = field(default_factory=list)  # live but not downloaded (Track or YouTubeTrack)
    no_longer_live: List[str] = field(default_factory=list)  # filenames present locally, not live anymore
    needs_renumber: List[str] = field(default_factory=list)  # track names whose position has shifted
    unchanged_count: int = 0
    # v0.16.27: {track key: track_failures record} for the missing tracks
    # that failed on an earlier sync (read only, nothing is written).
    known_failures: Dict[str, dict] = field(default_factory=dict)
    # v0.16.39: as SyncResult.unavailable_on_spotify.
    unavailable_on_spotify: List[object] = field(default_factory=list)

    @property
    def total_live_tracks(self) -> int:
        return len(self.missing) + len(self.needs_renumber) + self.unchanged_count


@dataclass
class IntegrityResult:
    """
    Read-only report on a playlist folder's file-level health --
    Checklist item 3. Distinct from ScanResult: this never talks to
    Spotify or YouTube at all, and never compares against the live
    playlist -- it only looks at what's actually sitting on disk.
    """
    playlist_folder: str
    orphaned_temp_files: List[Path] = field(default_factory=list)  # leftovers from an interrupted crash
    unreadable_files: List[str] = field(default_factory=list)  # song files mutagen can't open at all
    untagged_files: List[str] = field(default_factory=list)  # readable, but no identity tag

    @property
    def clean(self) -> bool:
        return not (self.orphaned_temp_files or self.unreadable_files or self.untagged_files)


def format_duration(seconds: float) -> str:
    """'45s', '4m 12s' or '1h 05m' (v0.17.6): the Sync Log's and the Sync
    Progress panel's way of writing a length of time."""
    seconds = int(round(max(0.0, seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class _SyncClock:
    """How long one playlist's sync takes (v0.17.6, Checklist item 57).
    Its report() passes the progress messages on and leaves the time spent
    inside the caller's callback out of the total: that is where the GUI's
    Pause waits, and otherwise the callback returns at once."""

    def __init__(self, on_progress: Optional[Callable[[str], None]]):
        self._on_progress = on_progress
        self._start = time.monotonic()
        self._waiting = 0.0

    def report(self, message: str) -> None:
        if not self._on_progress:
            return
        t = time.monotonic()
        try:
            self._on_progress(message)
        finally:
            self._waiting += time.monotonic() - t

    def seconds(self) -> float:
        return max(0.0, time.monotonic() - self._start - self._waiting)


def _put_log_source(log_path: Path, link: str) -> None:
    """Keep "Source: <link>" as the Sync Log's first line, once (v0.17.6,
    Checklist item 57). A log from before v0.17.6 gets it added on top; a
    playlist whose link was changed gets the line replaced, so the log
    always names the source it is synced from now."""
    try:
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    except OSError as e:
        logger.log_error(f"Failed to read the download log at {log_path}: {e}")
        return
    first, _, rest = existing.partition("\n")
    if first.startswith(LOG_SOURCE_PREFIX):
        if first[len(LOG_SOURCE_PREFIX):].strip() == link:
            return
        existing = rest.lstrip("\n")
    text = f"{LOG_SOURCE_PREFIX}{link}\n\n{existing}"
    tmp_path = log_path.with_name(log_path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, log_path)
    except OSError as e:
        logger.log_error(f"Failed to write the source line of the download log at {log_path}: {e}")


def _write_download_log(folder: Path, result: "SyncResult", link: Optional[str] = None) -> None:
    """
    Append a timestamped entry to this playlist's own Sync Log.txt,
    living inside its folder (separate from the app-wide logs/
    directory) -- date/time, how many new tracks succeeded/failed out
    of how many attempted, and exactly which ones failed and why.
    Only written when this run actually attempted any new downloads.

    v0.17.6 (Checklist item 57): each entry says how long the sync took
    (time paused left out) and the average time per download; the file's
    first line is the playlist's source link (_put_log_source()).
    """
    log_path = Path(folder) / DOWNLOAD_LOG_FILENAME
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if link:
        _put_log_source(log_path, link)

    total_attempted = len(result.downloaded)
    failed_downloads = [r for r in result.downloaded if r.file_path is None]
    succeeded = total_attempted - len(failed_downloads)

    duration = f"Duration: {format_duration(result.elapsed_seconds)}"
    if total_attempted:
        duration += (f" (downloading {format_duration(result.download_seconds)}, "
                     f"{result.download_seconds / total_attempted:.1f}s per song on average)")

    lines = [
        f"=== Sync: {timestamp} ===",
        duration,
        f"New downloads: {succeeded}/{total_attempted} succeeded",
        f"Renumbered: {len(result.renumbered)}   Removed: {len(result.removed)}   "
        f"Unchanged: {result.unchanged_count}",
    ]

    if result.failure_details:
        lines.append("")
        lines.append("Failed:")
        for d in result.failure_details:
            lines.append(f'  - "{d.get("title", "")}" by {d.get("artist", "")}: {track_failures.summary(d)}')

    lines.append("")  # blank line separating entries

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        logger.log_error(f"Failed to write download log at {log_path}: {e}")


def _scan_existing_files(folder: Path) -> dict:
    """
    Scan a playlist folder for song files (any format, v0.16.45) and read each one's identity
    string from its Comment tag. Returns {identity_id: file_path}.
    Platform-neutral: the identity string is whatever raw value is in
    Comment -- a bare Spotify Track ID or a "yt:<videoId>" YouTube
    identity -- and the caller compares it against a set of live IDs
    built the same way for its own platform.

    Only looks at folder's direct children (non-recursive), so anything
    sitting inside the `.mmm_staging` staging subfolder (see
    acquisition_engine.py) is automatically invisible here -- it isn't
    "real" yet either way.

    Files with no readable identity tag (e.g. something dropped into
    the folder manually, or from before this tagging convention
    existed) are skipped and left completely untouched by the diff --
    on purpose, since we can't safely identify what they are.
    """
    existing = {}
    if not folder.exists():
        return existing

    for file_path in metadata_tagger.audio_files(folder):
        identity_id = metadata_tagger.read_track_identity(file_path)
        if identity_id:
            existing[identity_id] = file_path
        else:
            logger.log_event(f"Skipping untagged/unrecognized file during sync: {file_path.name}")

    return existing


def position_total(live_tracks, unavailable=()) -> int:
    """The "of total" for Track # (v0.16.37): the highest Spotify
    position, counting the slots of items Spotify gives no details for
    (v0.16.39: also when they are the last items of the playlist)."""
    positions = [t.list_position for t in live_tracks] + [u.list_position for u in unavailable]
    return max(positions + [len(live_tracks)])


def _keep_imported_unavailable(live_tracks, unavailable: list, existing_by_id: dict) -> list:
    """Items unavailable on Spotify whose file the user imported (v0.16.40,
    Checklist item 47) count as live: they are taken out of `unavailable`
    (in place) and returned among the live tracks, at their position, as
    a Track built from the listing slot (its name from the file). The
    diff then renumbers them and never removes them."""
    kept = [u for u in unavailable if u.spotify_id and u.spotify_id in existing_by_id]
    if not kept:
        return live_tracks
    unavailable[:] = [u for u in unavailable if u not in kept]
    extra = [spotify_reader.Track(spotify_id=u.spotify_id, name=u.name or existing_by_id[u.spotify_id].stem,
                                  list_position=u.list_position) for u in kept]
    return sorted(list(live_tracks) + extra, key=lambda t: t.list_position)


def _track_total(track, default: Optional[int]) -> Optional[int]:
    """Track #'s "of total" for this track: an album track's own disc's
    track count (v0.16.48), else the playlist's total (`default`)."""
    return getattr(track, "track_total", 0) or default


def _current_track_number(file_path: Path) -> Optional[int]:
    tags = metadata_tagger.read_tags(file_path)  # any format (v0.16.45); logs a read failure
    if tags is None:
        return None
    return tags.track_number


def _finish_sync(result: SyncResult, folder: Path, report: Callable[[str], None],
                 entry: Optional[dict] = None, clock: Optional[_SyncClock] = None) -> SyncResult:
    """Shared tail of both platforms' sync: log/report the summary and
    write the per-playlist download log if anything was attempted.
    v0.17.6: the summary and the log say how long it took (clock)."""
    if clock is not None:
        result.elapsed_seconds = clock.seconds()
    summary = (
        f"Sync complete: {len(result.downloaded)} downloaded, "
        f"{len(result.renumbered)} renumbered, {len(result.removed)} removed, "
        f"{result.unchanged_count} unchanged, {len(result.failed)} failed/unmatched"
    )
    if clock is not None:
        summary += f" -- took {format_duration(result.elapsed_seconds)}"
    logger.log_event(f"{summary} (folder: '{folder.name}')")
    report(summary)

    if result.downloaded:
        link = None
        if entry is not None:
            try:
                link = config_manager.entry_link(entry)
            except Exception as e:  # never lose the log entry over its header line
                logger.log_error(f"Couldn't build the source link for the Sync Log of '{folder.name}': {e}")
        _write_download_log(folder, result, link)

    return result


def _remove_no_longer_live(
    existing_by_id: dict, live_ids: set, result: SyncResult, report: Callable[[str], None]
) -> None:
    """
    Removals: any existing file whose identity is no longer live. Each
    unlink below is independent and atomic at the OS level, and this
    only ever runs after every download has already been attempted --
    so an interruption here just leaves whichever no-longer-live files
    hadn't been reached yet for the next sync to catch, with no
    partial/corrupted state possible (reviewed under Checklist item
    3; see the module docstring).
    """
    for identity_id, file_path in existing_by_id.items():
        if identity_id not in live_ids:
            try:
                file_path.unlink()
                result.removed.append(file_path.name)
                logger.log_event(f"Removed '{file_path.name}' -- no longer in the live playlist")
                report(f"Removed '{file_path.name}' -- no longer in the live playlist.")
            except OSError as e:
                logger.log_error(f"Failed to remove {file_path}: {e}")


def _downloaded_video_ids(existing_by_id: dict) -> set:
    """Bare video IDs of the YouTube-native files already in a folder
    (their Comment identity minus the "yt:" prefix), for
    get_youtube_playlist_tracks()'s skip_details_for."""
    prefix = metadata_tagger.YOUTUBE_IDENTITY_PREFIX
    return {i[len(prefix):] for i in existing_by_id if i.startswith(prefix)}


def _song_key(track) -> str:
    """The exceptions / failures key of a song whose source is known (not
    searched for): its bare video ID, or (v0.16.52) its SoundCloud track
    ID."""
    return getattr(track, "soundcloud_id", None) or track.video_id


def _song_identity(track) -> str:
    """The Comment identity of such a song: "yt:<videoId>" or (v0.16.52)
    "sc:<track id>"."""
    sc_id = getattr(track, "soundcloud_id", None)
    if sc_id:
        return metadata_tagger.build_soundcloud_identity(sc_id)
    return metadata_tagger.build_youtube_identity(track.video_id)


def _acquire_safely(acquire, track, folder: Path, **kwargs) -> acquisition_engine.AcquisitionResult:
    """Run one track's acquisition. An unexpected error in it (a bug, an
    odd library failure) is reported as that track's "App error" instead
    of stopping the whole playlist's sync (v0.16.27)."""
    try:
        return acquire(track, folder, **kwargs)
    except Exception as e:
        logger.log_error(
            f"Unexpected error acquiring '{track.primary_artist} - {track.name}': "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        return acquisition_engine.AcquisitionResult(
            track=track, match=None, file_path=None, tagged=False, has_lyrics=False,
            failure_reason=f"{type(e).__name__}: {e}", failure_kind=track_failures.APP_ERROR,
        )


def _acquire_timed(result: SyncResult, acquire, track, folder: Path, **kwargs) -> acquisition_engine.AcquisitionResult:
    """_acquire_safely(), adding the time it took to result.download_seconds
    (v0.17.6, for the Sync Log's average per download)."""
    t = time.monotonic()
    try:
        return _acquire_safely(acquire, track, folder, **kwargs)
    finally:
        result.download_seconds += time.monotonic() - t


def _report_to_download(count: int, max_new_downloads: Optional[int], report: Callable[[str], None]) -> None:
    """Say how many songs this sync will download, before the first one
    starts (v0.17.6): the GUI's ETA counts on it."""
    if max_new_downloads is not None:
        count = min(count, max_new_downloads)
    report(f"{count} new {'track' if count == 1 else 'tracks'} to download.")


def _record_outcome(
    playlist_id: str,
    track_key: str,
    acquisition_result: acquisition_engine.AcquisitionResult,
    result: SyncResult,
    index: int,
    total_tracks: int,
    report: Callable[[str], None],
) -> None:
    """Shared by both platforms' sync after each download attempt:
    remember or forget the track's failure (track_failures, v0.16.27),
    add it to the result and report it."""
    track = acquisition_result.track
    if acquisition_result.file_path is not None:
        track_failures.clear(playlist_id, track_key)
        report(f"[{index}/{total_tracks}] Done ({acquisition_result.match.tier}). ✅")
        return

    name = f"{track.primary_artist} - {track.name}"
    record = track_failures.record_failure(
        playlist_id, track_key,
        acquisition_result.failure_kind or track_failures.DOWNLOAD_ERROR,
        acquisition_result.failure_reason or "unknown reason",
        title=track.name, artist=track.primary_artist,
    )
    text = track_failures.summary(record)
    result.failed.append((name, text))
    result.failure_details.append(dict(record, key=track_key, name=name))
    report(f"[{index}/{total_tracks}] FAILED: {text}")


def _sync_spotify_playlist(
    entry: dict,
    folder: Path,
    max_new_downloads: Optional[int],
    on_progress: Optional[Callable[[str], None]],
    clock: Optional[_SyncClock] = None,
) -> SyncResult:
    """Spotify-native sync -- unchanged behavior from before v0.15.0,
    just receiving the whole config entry instead of a bare ID."""
    def report(message: str) -> None:
        if on_progress:
            on_progress(message)

    spotify_playlist_id = config_manager.get_playlist_source_id(entry)
    album = config_manager.is_album(entry)  # v0.16.48 (Checklist item 41a)

    folder.mkdir(parents=True, exist_ok=True)
    result = SyncResult(playlist_folder=str(folder))

    # Local files are read first (Checklist item 26) so the live read
    # can skip the detail lookup for tracks already downloaded.
    existing_by_id = _scan_existing_files(folder)

    report(f"Fetching live {'album' if album else 'playlist'} data from "
           f"{config_manager.platform_name(config_manager.get_playlist_platform(entry))} "
           f"(this can take a minute)...")
    # v0.16.53 (Checklist item 42c): a Spotify or an Apple Music entry.
    live_tracks = acquisition_engine.get_catalog_tracks(
        entry, skip_details_for=existing_by_id.keys(), unavailable=result.unavailable_on_spotify,
    )
    live_tracks = _keep_imported_unavailable(live_tracks, result.unavailable_on_spotify, existing_by_id)
    total_tracks = len(live_tracks)
    # Track # "N of total" uses the Spotify position count: an item
    # Spotify gives no details for (not available in this country) keeps
    # its slot, so positions can run past total_tracks (v0.16.37).
    tracks_total = position_total(live_tracks, result.unavailable_on_spotify)
    live_ids = {track.spotify_id for track in live_tracks}
    report(f"Found {total_tracks} tracks in the live {'album' if album else 'playlist'}.")
    _report_to_download(sum(1 for t in live_tracks if t.spotify_id not in existing_by_id),
                        max_new_downloads, report)

    new_downloads_done = 0

    for index, track in enumerate(live_tracks, start=1):
        existing_path = existing_by_id.get(track.spotify_id)

        if existing_path is None:
            if max_new_downloads is not None and new_downloads_done >= max_new_downloads:
                continue  # cap reached for this run -- a future sync will pick this up
            report(f"[{index}/{total_tracks}] Downloading '{track.name}'...")
            override = exceptions_manager.get_exception(spotify_playlist_id, track.spotify_id)
            acquisition_result = _acquire_timed(
                result, acquisition_engine.acquire_track,
                track, folder, total_tracks=_track_total(track, tracks_total), override_youtube_id=override
            )
            result.downloaded.append(acquisition_result)
            new_downloads_done += 1
            _record_outcome(
                spotify_playlist_id, track.spotify_id, acquisition_result, result,
                index, total_tracks, report,
            )
            continue

        # Already have it -- only check whether its position changed.
        current_number = _current_track_number(existing_path)
        if current_number != track.list_position:
            success = metadata_tagger.update_track_number(existing_path, track.list_position,
                                                          _track_total(track, tracks_total))
            if success:
                result.renumbered.append(track.name)
            else:
                result.failed.append((f"{track.primary_artist} - {track.name}", TRACK_NUMBER_FAILURE))
        else:
            result.unchanged_count += 1

    _remove_no_longer_live(existing_by_id, live_ids, result, report)
    track_failures.prune(spotify_playlist_id, live_ids)
    return _finish_sync(result, folder, report, entry, clock)


def _sync_youtube_playlist(
    entry: dict,
    folder: Path,
    max_new_downloads: Optional[int],
    on_progress: Optional[Callable[[str], None]],
    clock: Optional[_SyncClock] = None,
) -> SyncResult:
    """
    YouTube-native sync (Checklist item 6, v0.15.0). Same diff/removal/
    renumber structure as _sync_spotify_playlist(), but the live track
    list comes from youtube_playlist_reader, each track's identity
    string is "yt:<videoId>", and a missing track is downloaded
    directly via acquire_youtube_native_track() -- no search tiers
    (design Q3). A saved exception (design Q4) is keyed by the
    playlist's ORIGINAL video ID for that entry, and swaps in a
    different video to actually download while the file's identity
    stays the original -- see acquire_youtube_native_track().

    v0.16.52 (Checklist item 42b): a SoundCloud set is synced here too --
    the same diff, with "sc:<track id>" identities, the songs read by
    soundcloud_reader and downloaded by acquire_soundcloud_track().
    """
    def report(message: str) -> None:
        if on_progress:
            on_progress(message)

    youtube_playlist_id = config_manager.get_playlist_source_id(entry)
    platform = config_manager.get_playlist_platform(entry)
    ytmusic = platform == config_manager.PLATFORM_YTMUSIC  # v0.16.51
    soundcloud = platform == config_manager.PLATFORM_SOUNDCLOUD  # v0.16.52
    album = config_manager.is_album(entry)

    folder.mkdir(parents=True, exist_ok=True)
    result = SyncResult(playlist_folder=str(folder))

    # Local files are read first (Checklist item 26) so the live read
    # only fetches full details for videos not downloaded yet.
    existing_by_id = _scan_existing_files(folder)

    if ytmusic or soundcloud:
        report(f"Fetching live {'album' if album else 'playlist'} data from "
               f"{config_manager.platform_name(platform)}...")
    else:
        report(
            "Fetching live playlist data from YouTube (this can take a while -- "
            "each new video's details are fetched individually)..."
        )
    if soundcloud:
        live_tracks = acquisition_engine.get_soundcloud_tracks(entry)
        acquire = acquisition_engine.acquire_soundcloud_track
    else:
        live_tracks = acquisition_engine.get_video_tracks(
            entry, skip_details_for=_downloaded_video_ids(existing_by_id)
        )
        acquire = acquisition_engine.acquire_youtube_native_track
    total_tracks = len(live_tracks)
    live_ids = {_song_identity(track) for track in live_tracks}
    report(f"Found {total_tracks} {'tracks' if ytmusic or soundcloud else 'videos'} in the live "
           f"{'album' if album else 'playlist'}.")
    _report_to_download(sum(1 for t in live_tracks if _song_identity(t) not in existing_by_id),
                        max_new_downloads, report)

    new_downloads_done = 0

    for index, track in enumerate(live_tracks, start=1):
        identity_id = _song_identity(track)
        existing_path = existing_by_id.get(identity_id)

        if existing_path is None:
            if max_new_downloads is not None and new_downloads_done >= max_new_downloads:
                continue  # cap reached for this run -- a future sync will pick this up
            report(f"[{index}/{total_tracks}] Downloading '{track.name}'...")
            override = exceptions_manager.get_exception(youtube_playlist_id, _song_key(track))
            acquisition_result = _acquire_timed(
                result, acquire, track, folder, total_tracks=_track_total(track, total_tracks), override_video_id=override
            )
            result.downloaded.append(acquisition_result)
            new_downloads_done += 1
            _record_outcome(
                youtube_playlist_id, _song_key(track), acquisition_result, result,
                index, total_tracks, report,
            )
            continue

        # Already have it -- only check whether its position changed.
        current_number = _current_track_number(existing_path)
        if current_number != track.list_position:
            success = metadata_tagger.update_track_number(existing_path, track.list_position,
                                                          _track_total(track, total_tracks))
            if success:
                result.renumbered.append(track.name)
            else:
                result.failed.append((f"{track.primary_artist} - {track.name}", TRACK_NUMBER_FAILURE))
        else:
            result.unchanged_count += 1

    _remove_no_longer_live(existing_by_id, live_ids, result, report)
    track_failures.prune(youtube_playlist_id, {_song_key(track) for track in live_tracks})
    return _finish_sync(result, folder, report, entry, clock)


def sync_playlist(
    entry: dict,
    folder: Path,
    max_new_downloads: Optional[int] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> SyncResult:
    """
    Sync a single tracked playlist's local folder against its live
    state -- Spotify-native or YouTube-native, dispatched on the
    entry's `platform` field (Checklist item 6, v0.15.0). Downloads new
    tracks, removes tracks no longer in the playlist, and renumbers
    tracks whose position changed -- all without re-downloading
    anything already present.

    entry: the playlist's playlists.json entry (not a bare playlist ID
    string, as this took before v0.15.0 -- the whole entry is needed
    now to know which platform to read from).

    max_new_downloads: mainly a testing convenience -- caps how many
    NEW tracks get downloaded in this call (e.g. max_new_downloads=3
    to try it on a big playlist without downloading all of it). Tracks
    beyond the cap are simply left for a future sync to pick up. This
    does NOT affect renumbering or removal -- those still run against
    the full live playlist state every time, so a capped test run is
    still safe to use on a real playlist/folder.

    on_progress: optional callback receiving short human-readable status
    strings (e.g. "[3/87] Downloading 'Song Name'..."), so a caller (like
    main.py) can print something during what can otherwise be a long
    silent wait. Purely cosmetic -- has no effect on sync behavior, and
    is a no-op if not provided.

    v0.17.6 (Checklist item 57): the sync is timed (_SyncClock, time spent
    inside on_progress -- where the GUI's Pause waits -- left out) for the
    result's elapsed_seconds, the summary and the Sync Log. Before the
    first download, on_progress also gets "N new tracks to download."
    """
    folder = Path(folder)
    clock = _SyncClock(on_progress)
    if config_manager.uses_video_ids(entry) or config_manager.is_soundcloud(entry):
        # YouTube, YT Music (v0.16.51) and SoundCloud (v0.16.52): known songs, no search
        return _sync_youtube_playlist(entry, folder, max_new_downloads, clock.report, clock)
    return _sync_spotify_playlist(entry, folder, max_new_downloads, clock.report, clock)


def sync_all_tracked_playlists(
    config: Optional[dict] = None,
    max_new_downloads: Optional[int] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[SyncResult]:
    """
    Sync every playlist currently tracked in playlists.json. Convenience
    wrapper over sync_playlist() for the common "sync everything" case.

    Each playlist's folder is resolved via config_manager.get_playlist_folder()
    (its custom_path if it has one, otherwise <library_path>/<folder_name>).
    An entry that can't be resolved (no custom_path and no library folder
    set) is skipped individually with a message, rather than failing the
    whole run.
    """
    if config is None:
        config = config_manager.load_config()

    def report(message: str) -> None:
        if on_progress:
            on_progress(message)

    results = []
    for entry in config_manager.list_playlists(config):
        report(f"\n=== Syncing '{entry['folder_name']}' ===")
        try:
            folder = config_manager.get_playlist_folder(config, entry)
        except ValueError as e:
            report(f"Skipped '{entry['folder_name']}': {e}")
            logger.log_error(f"Skipped syncing '{entry['folder_name']}': {e}")
            continue

        result = sync_playlist(
            entry, folder, max_new_downloads=max_new_downloads, on_progress=on_progress
        )
        results.append(result)

    return results


def _scan_diff(
    live_pairs: List[Tuple[str, object]],
    folder: Path,
    result: ScanResult,
    report: Callable[[str], None],
    existing_by_id: Optional[dict] = None,
) -> ScanResult:
    """
    Shared read-only diff for both platforms' Scan: live_pairs is
    [(identity_id, track)] in live order. Never downloads, deletes, or
    tags anything. existing_by_id: the folder's files if the caller
    already read them (Checklist item 26), otherwise read here.
    """
    live_ids = {identity_id for identity_id, _ in live_pairs}
    if existing_by_id is None:
        existing_by_id = _scan_existing_files(folder)

    for identity_id, track in live_pairs:
        existing_path = existing_by_id.get(identity_id)

        if existing_path is None:
            result.missing.append(track)
            continue

        # Already have it -- just check whether its position has drifted.
        current_number = _current_track_number(existing_path)
        if current_number != track.list_position:
            result.needs_renumber.append(track.name)
        else:
            result.unchanged_count += 1

    # Anything local whose identity is no longer in the live playlist.
    for identity_id, file_path in existing_by_id.items():
        if identity_id not in live_ids:
            result.no_longer_live.append(file_path.name)

    summary = (
        f"Scan complete: {len(result.missing)} missing locally, "
        f"{len(result.needs_renumber)} would be renumbered, "
        f"{len(result.no_longer_live)} no longer live, "
        f"{result.unchanged_count} unchanged"
    )
    logger.log_event(f"{summary} (folder: '{folder.name}')")
    report(summary)

    return result


def scan_playlist(
    entry: dict,
    folder: Path,
    on_progress: Optional[Callable[[str], None]] = None,
) -> ScanResult:
    """
    Read-only dry-run of sync_playlist(): walks the exact same diff
    (live playlist vs. local folder contents, keyed on the identity
    string burned into each file's Comment tag -- see design doc
    Section 3.3) but never downloads, deletes, or tags anything. Safe
    to run at any time as a pure inspection tool. Works for either
    platform, dispatched on the entry's `platform` field (Checklist
    item 6, v0.15.0); like sync_playlist(), it now takes the whole
    config entry rather than a bare ID string.

    Unlike sync_playlist(), this does NOT create the playlist's folder
    if it doesn't exist yet -- a missing folder just means everything
    live is reported as missing locally, which is already the correct,
    truthful answer for a read-only scan.

    For a YouTube-native playlist, Scan skips the expensive per-video
    detail fetch a real Sync does (see
    youtube_playlist_reader.get_playlist_tracks()'s fetch_details) --
    it only needs to know which videos are live and in what order, so
    a missing track's displayed artist may be just its channel name
    here even if a real Sync would later scrape a better one.

    For a Spotify playlist (Checklist item 26, v0.16.19), the folder is
    read first and only tracks not downloaded yet get the per-track
    detail lookup -- the ones already downloaded only need their ID,
    name and position here. The report is the same as before.

    on_progress: same optional progress-callback convenience as
    sync_playlist() -- purely cosmetic, no effect on behavior.
    """
    def report(message: str) -> None:
        if on_progress:
            on_progress(message)

    folder = Path(folder)
    result = ScanResult(playlist_folder=str(folder))
    platform = config_manager.get_playlist_platform(entry)
    source_id = config_manager.get_playlist_source_id(entry)

    if config_manager.uses_video_ids(entry) or config_manager.is_soundcloud(entry):
        # YouTube, YT Music (v0.16.51) and SoundCloud (v0.16.52)
        album = config_manager.is_album(entry)
        report(f"Fetching live {'album' if album else 'playlist'} data from "
               f"{config_manager.platform_name(platform)}...")
        if config_manager.is_soundcloud(entry):
            live_tracks = acquisition_engine.get_soundcloud_tracks(entry)
        else:
            live_tracks = acquisition_engine.get_video_tracks(entry, fetch_details=False)
        live_pairs = [(_song_identity(t), t) for t in live_tracks]
        existing_by_id = None
    else:
        existing_by_id = _scan_existing_files(folder)
        album = config_manager.is_album(entry)  # v0.16.48 (Checklist item 41a)
        report(f"Fetching live {'album' if album else 'playlist'} data from "
               f"{config_manager.platform_name(platform)} (this can take a minute)...")
        live_tracks = acquisition_engine.get_catalog_tracks(  # Spotify or Apple Music (v0.16.53)
            entry, skip_details_for=existing_by_id.keys(), unavailable=result.unavailable_on_spotify,
        )
        live_tracks = _keep_imported_unavailable(live_tracks, result.unavailable_on_spotify, existing_by_id)
        live_pairs = [(t.spotify_id, t) for t in live_tracks]

    report(f"Found {len(live_tracks)} tracks in the live {'album' if config_manager.is_album(entry) else 'playlist'}.")
    _scan_diff(live_pairs, folder, result, report, existing_by_id)

    # v0.16.27: what earlier syncs recorded about the missing tracks
    # (read only -- a Scan never changes track_failures.json).
    if result.missing:
        recorded = track_failures.get_playlist(source_id)
        for track in result.missing:
            key = track_failures.track_key(track)
            if key in recorded:
                result.known_failures[key] = recorded[key]
    return result


def scan_all_tracked_playlists(
    config: Optional[dict] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[ScanResult]:
    """
    Read-only convenience wrapper over scan_playlist() for every
    playlist tracked in playlists.json -- mirrors
    sync_all_tracked_playlists(), but never touches any files.
    """
    if config is None:
        config = config_manager.load_config()

    def report(message: str) -> None:
        if on_progress:
            on_progress(message)

    results = []
    for entry in config_manager.list_playlists(config):
        report(f"\n=== Scanning '{entry['folder_name']}' ===")
        try:
            folder = config_manager.get_playlist_folder(config, entry)
        except ValueError as e:
            report(f"Skipped '{entry['folder_name']}': {e}")
            logger.log_error(f"Skipped scanning '{entry['folder_name']}': {e}")
            continue

        result = scan_playlist(entry, folder, on_progress=on_progress)
        results.append(result)

    return results


def check_playlist_integrity(folder: Path) -> IntegrityResult:
    """
    Read-only inspection of a playlist folder for signs of a prior
    crash or interruption (Checklist item 3), and for files a normal
    Sync/Scan silently can't identify:

      - Leftover files in the `.mmm_staging` staging subfolder, or a
        stray `*<RETAG_TEMP_SUFFIX>` file directly in the folder -- a
        download, tagging, or renumbering step that was killed
        mid-way (crash, forced close) before its result could be
        committed. These are never picked up by a normal sync (they're
        deliberately invisible to _scan_existing_files(), which only
        looks at the folder's direct song files).
      - Song files (.m4a, and since v0.16.45 .mp3 / .ogg / .flac / .wav)
        whose tags can't be read at all (corrupted file).
      - Song files that read fine but have no identity string in
        their Comment field -- these are silently skipped by every
        sync/scan (see _scan_existing_files()), which is correct
        behavior for a file dropped in on purpose, but worth
        surfacing here since it can also be the result of a tagging
        step that was interrupted before the Comment field was ever
        written. Only the Comment tag's presence is checked, not its
        contents, so this works unchanged for Spotify- and
        YouTube-native folders alike.

    Never deletes or modifies anything -- pairs with Scan (Checklist
    item 2) as a second "inspect without acting" tool, this time for
    file integrity rather than the live-vs-local diff. See
    clean_orphaned_temp_files() for the separate, explicit cleanup step.
    """
    folder = Path(folder)
    result = IntegrityResult(playlist_folder=str(folder))

    if not folder.exists():
        return result

    staging_dir = folder / STAGING_SUBDIR_NAME
    if staging_dir.exists():
        result.orphaned_temp_files.extend(sorted(staging_dir.glob("*")))

    result.orphaned_temp_files.extend(sorted(folder.glob(f"*{RETAG_TEMP_SUFFIX}")))

    for file_path in metadata_tagger.audio_files(folder):
        tags = metadata_tagger.read_tags(file_path)
        if tags is None:
            result.unreadable_files.append(file_path.name)
            continue

        if not tags.identity:
            result.untagged_files.append(file_path.name)

    return result


def check_all_tracked_playlists_integrity(config: Optional[dict] = None) -> List[Tuple[dict, IntegrityResult]]:
    """
    Read-only convenience wrapper over check_playlist_integrity() for
    every playlist tracked in playlists.json. Returns (entry, result)
    pairs rather than just results, since -- unlike Scan/Sync -- the
    caller usually needs the entry back too, to offer a targeted
    cleanup afterward.
    """
    if config is None:
        config = config_manager.load_config()

    pairs = []
    for entry in config_manager.list_playlists(config):
        try:
            folder = config_manager.get_playlist_folder(config, entry)
        except ValueError as e:
            logger.log_error(f"Skipped integrity check for '{entry['folder_name']}': {e}")
            continue
        pairs.append((entry, check_playlist_integrity(folder)))

    return pairs


def clean_orphaned_temp_files(folder: Path) -> int:
    """
    Deletes every leftover file identified by check_playlist_integrity()
    as an orphaned temp file: everything inside the playlist's
    `.mmm_staging` subfolder, plus any stray `*<RETAG_TEMP_SUFFIX>`
    file sitting directly in the folder. This is the *acting* half of
    the integrity check, kept as a separate, explicit call so
    inspecting integrity never has a side effect on its own (Checklist
    item 3, matching the Scan/Sync split from item 2). Returns how many
    files were removed.
    """
    folder = Path(folder)
    removed = 0

    staging_dir = folder / STAGING_SUBDIR_NAME
    if staging_dir.exists():
        for file_path in staging_dir.glob("*"):
            try:
                file_path.unlink()
                removed += 1
            except OSError as e:
                logger.log_error(f"Failed to remove orphaned staged file {file_path}: {e}")
        try:
            staging_dir.rmdir()
        except OSError:
            pass  # not empty (a removal above failed) or already gone -- fine

    for file_path in folder.glob(f"*{RETAG_TEMP_SUFFIX}"):
        try:
            file_path.unlink()
            removed += 1
        except OSError as e:
            logger.log_error(f"Failed to remove orphaned retag temp file {file_path}: {e}")

    if removed:
        logger.log_event(f"Removed {removed} orphaned temp file(s) from '{folder.name}'")

    return removed


def fix_track(
    track,
    entry: dict,
    folder: Path,
    youtube_url_or_id: str,
    total_tracks: Optional[int] = None,
) -> acquisition_engine.AcquisitionResult:
    """
    Correct a track that already has a bad download sitting in the
    folder (or that never matched at all): saves the given link as a
    permanent exception for this track in this playlist (so future
    syncs use it automatically), removes the existing file for this
    track if one is present, and immediately re-downloads and tags it
    with the corrected video.

    Works for either platform (Checklist item 6, design Q4 --
    exceptions still apply to YouTube-native tracks, e.g. to sub out a
    link): `entry` is the playlist's whole config entry (this used to
    take a bare Spotify playlist ID string before v0.15.0 -- a
    breaking signature change for any external script calling it
    directly), and `track` is a spotify_reader.Track or a
    youtube_playlist_reader.YouTubeTrack to match. For a YouTube-native
    playlist the exception is keyed by the playlist entry's ORIGINAL
    video ID, and the re-downloaded file keeps that original ID as its
    identity while the override link is what actually gets downloaded
    -- see acquisition_engine.acquire_youtube_native_track().

    Raises ValueError if youtube_url_or_id isn't a recognizable YouTube
    link or video ID (nothing is deleted or changed in that case).
    """
    is_youtube_native = config_manager.uses_video_ids(entry)  # YT Music too (v0.16.51)
    soundcloud = config_manager.is_soundcloud(entry)  # v0.16.52
    playlist_id = config_manager.get_playlist_source_id(entry)

    if is_youtube_native or soundcloud:
        track_key = _song_key(track)
        identity_id = _song_identity(track)
    else:
        track_key = track.spotify_id
        identity_id = track.spotify_id

    video_id = exceptions_manager.add_exception(playlist_id, track_key, youtube_url_or_id)
    total_tracks = _track_total(track, total_tracks)  # v0.16.48: an album track's disc total

    # v0.16.38 (Checklist item 44): the old file is only removed once the
    # new download has been committed (it used to be removed first, so an
    # exception link that couldn't be downloaded -- e.g. an age-restricted
    # video -- left the song with no file at all). The commit replaces an
    # old file of the same name itself (acquisition_engine._final_path()).
    folder = Path(folder)
    old_files = [p for p in metadata_tagger.audio_files(folder)
                 if metadata_tagger.read_track_identity(p) == identity_id]

    if soundcloud:
        acquisition_result = acquisition_engine.acquire_soundcloud_track(
            track, folder, total_tracks=total_tracks, override_video_id=video_id
        )
    elif is_youtube_native:
        acquisition_result = acquisition_engine.acquire_youtube_native_track(
            track, folder, total_tracks=total_tracks, override_video_id=video_id
        )
    else:
        acquisition_result = acquisition_engine.acquire_track(
            track, folder, total_tracks=total_tracks, override_youtube_id=video_id
        )

    if acquisition_result.file_path is not None:
        for file_path in old_files:
            if file_path.resolve() == Path(acquisition_result.file_path).resolve():
                continue  # replaced in place by the new download
            try:
                file_path.unlink()
                logger.log_event(f"Removed '{file_path.name}' -- replaced via exception fix")
            except OSError as e:
                logger.log_error(f"Failed to remove {file_path} during exception fix: {e}")
    elif old_files:
        logger.log_event(
            f"Exception fix for '{track.name}' failed to download; kept the existing "
            f"'{old_files[0].name}'"
        )

    # v0.16.27: add_exception() already cleared the track's old failures;
    # a failed fix is recorded as its first failed attempt with the new link.
    if acquisition_result.file_path is None:
        track_failures.record_failure(
            playlist_id, track_key,
            acquisition_result.failure_kind or track_failures.DOWNLOAD_ERROR,
            acquisition_result.failure_reason or "unknown reason",
            title=track.name, artist=track.primary_artist,
        )
    return acquisition_result


def import_track(
    track,
    entry: dict,
    folder: Path,
    source_path: Path,
    total_tracks: Optional[int] = None,
) -> acquisition_engine.AcquisitionResult:
    """
    Use the user's own audio file for this track (v0.16.40, Checklist
    item 47): like fix_track(), but with a file instead of a link.
    `track` is a spotify_reader.Track or youtube_playlist_reader.
    YouTubeTrack (for a song Spotify gives no details for, a Track built
    from the typed title / artist at its position). The file is copied
    or converted, tagged and committed by acquisition_engine.
    import_audio_file(); the user's original is left where it was.

    Only once the new file is committed: the track's other files are
    removed (as fix_track()), its exception link is removed (the own
    file replaces it) and its recorded failures are cleared. If the
    import fails, nothing else changes and nothing is recorded in
    track_failures.json (an import isn't a sync attempt).
    """
    is_youtube_native = config_manager.uses_video_ids(entry)  # YT Music too (v0.16.51)
    playlist_id = config_manager.get_playlist_source_id(entry)
    if is_youtube_native or config_manager.is_soundcloud(entry):  # SoundCloud: v0.16.52
        track_key = _song_key(track)
        identity_id = _song_identity(track)
    else:
        track_key = track.spotify_id
        identity_id = track.spotify_id

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    old_files = [p for p in metadata_tagger.audio_files(folder)
                 if metadata_tagger.read_track_identity(p) == identity_id]
    total_tracks = _track_total(track, total_tracks)  # v0.16.48: an album track's disc total

    result = acquisition_engine.import_audio_file(track, source_path, folder, identity_id, total_tracks)
    if result.file_path is None:
        if old_files:
            logger.log_event(f"Import for '{track.name}' failed; kept the existing '{old_files[0].name}'")
        return result

    for file_path in old_files:
        if file_path.resolve() == Path(result.file_path).resolve():
            continue  # replaced in place by the imported file
        try:
            file_path.unlink()
            logger.log_event(f"Removed '{file_path.name}' -- replaced by an imported file")
        except OSError as e:
            logger.log_error(f"Failed to remove {file_path} after an import: {e}")
    if exceptions_manager.remove_exception(playlist_id, track_key):
        logger.log_event(f"Exception link for '{track.name}' removed: replaced by an imported file")
    track_failures.clear(playlist_id, track_key)
    return result
