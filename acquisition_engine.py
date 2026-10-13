"""
Mud Music Manager - Acquisition Engine

Orchestrates the modules that replace spotDL for this project, across
both platforms this app supports:
  - spotify_reader: reads live Spotify playlist data directly (no
    spotDL, no credentials, public playlists only)
  - youtube_playlist_reader: reads a YouTube-native playlist's live
    video list directly (Checklist item 6, v0.15.0)
  - youtube_matcher: finds the best YouTube match for a Spotify track
    (the tiered ATV -> plain YouTube -> OMV -> no-match strategy) --
    Spotify-sourced tracks only; per design Q3, a YouTube-native
    track is already the exact video to download, so this module does
    NOT run for those.
  - downloader: downloads a matched (or already-known) YouTube video's
    audio as m4a (direct yt-dlp, no forced re-encode), or since v0.16.45
    as the file format chosen in Settings (mp3 / ogg / flac / wav); the
    staged file keeps whatever extension the download got, and is tagged
    and committed the same way whatever its format

This is the only module the rest of the app should need to import for
acquiring tracks, for either platform. Keeping it as a thin orchestrator
over separate, independently-testable modules is deliberate -- see the
design doc, Section 2.2/2.3, for why spotDL was dropped and why this
stays isolated.

Checklist item 3 (crash / interruption failsafes) -- staged downloads:
acquire_track() and acquire_youtube_native_track() never download and
tag a track directly into its final, live playlist folder. Both
download and tag into a small `.mmm_staging` subfolder first, and only
*commit* the finished file into the real folder (via an atomic
os.replace()) once both the download and the tagging attempt are done.
If the process is killed at any point before that final move -- mid-
download, mid-tag-write -- the real playlist folder is left completely
untouched; at worst, a throwaway file is left behind in `.mmm_staging`,
invisible to normal Sync/Scan folder scans (which only look at a
folder's direct song files, metadata_tagger.audio_files()) and picked up instead by
library_sync.check_playlist_integrity()'s "Check Integrity" feature,
which can clean it up on request.

Own files (v0.16.40, Checklist item 47): import_audio_file() puts the
user's own audio file in the playlist as a song, with the same staging
and atomic commit: the file is copied (an .m4a) or converted with ffmpeg
(anything else) into `.mmm_staging`, tagged like a download (Grouping /
Composer say "Own file: <name>" instead of a YouTube ID), then committed.
The user's original file is only ever read.

Albums (v0.16.48, Checklist item 41a): get_playlist_tracks() and
get_playlist_tracks_with_details_for() take album=True to read a Spotify
album instead of a playlist (spotify_reader.get_album_tracks()), and
get_album_info() reads an album's name / artist for naming its folder.
Acquiring an album track is the same as a playlist track; its Track
carries the disc number, which tag_file() writes.

YT Music (v0.16.51, Checklist item 42a): ytmusic_reader reads YT Music
playlists, albums and artists. Their songs are YouTube videos
(YouTubeTrack), so get_video_tracks() reads a YouTube or a YT Music entry
and acquire_youtube_native_track() downloads either. One addition there:
an album song YT Music lists as a video (track.wants_audio_version) is
downloaded from its audio-only version instead when there is one
(Michael's choice) -- the one the reader found, else one found by
youtube_matcher.find_audio_version() -- with the file's identity still the
listed video. get_album_info() / get_artist_discography() read either
platform, by the shape of the ID (config_manager.id_platform()).

SoundCloud (v0.16.52, Checklist item 42b): soundcloud_reader reads
SoundCloud sets (playlists and albums). acquire_soundcloud_track()
downloads a song from SoundCloud itself; when SoundCloud only has a
30-second preview of it (or blocks it), or the download is refused as
DRM-protected, the song is found on YouTube with the usual tiered search
instead (Michael's choice: SoundCloud first, YouTube as the fallback). An
exception link (a YouTube link) overrides both, as everywhere. Same
staging and atomic commit as every download; the identity is
"sc:<track id>".

Apple Music (v0.16.53, Checklist item 42c): apple_music_reader reads
Apple Music playlists, albums and artists into spotify_reader.Track
objects (their spotify_id is the identity "am:<song id>"), so
acquire_track() -- the tiered YouTube search -- downloads them exactly as
it does Spotify songs (Michael's choice: Apple Music works like Spotify).
get_catalog_tracks(entry) reads a Spotify or an Apple Music entry.

Amazon Music (v0.16.54, Checklist item 42d): amazon_music_reader reads
Amazon Music playlists and albums the same way ("amz:<ASIN>"
identities). Amazon gives no song lengths, so the reader looks each new
song up in Apple's catalogue for one; a song Apple doesn't have is marked
match_without_length, and acquire_track() then accepts only a strict
official-audio match (youtube_matcher.find_strict_match(), Michael's
choice) instead of the tiered search.
"""

import os
import shutil
from pathlib import Path
from typing import Collection, List, NamedTuple, Optional

from mutagen.mp4 import MP4

import amazon_music_reader
import apple_music_reader
import config_manager
import soundcloud_reader
import spotify_reader
import youtube_playlist_reader
import youtube_matcher
import ytmusic_reader
import downloader
import metadata_tagger
import lyrics_provider
import logger
import track_failures

# Subfolder (inside a playlist's real folder) that downloads and
# tagging are staged into before being committed. Leading dot keeps it
# out of the way and out of the metadata_tagger.audio_files() results used by
# library_sync's normal diff scan. Exposed at module level so
# library_sync.py's corrupted/mismatched-data scan can look inside it
# for anything left behind by a crash.
STAGING_SUBDIR_NAME = ".mmm_staging"


class AcquisitionResult(NamedTuple):
    track: object  # spotify_reader.Track or youtube_playlist_reader.YouTubeTrack
    match: Optional[youtube_matcher.MatchResult]
    file_path: Optional[Path]
    tagged: bool
    has_lyrics: bool
    failure_reason: Optional[str] = None
    # v0.16.27 (Checklist item 28): which kind of failure, one of
    # track_failures' kinds (no match, download blocked, ...); None on
    # success.
    failure_kind: Optional[str] = None


def _make_staging_dir(output_dir: Path) -> Path:
    """The `.mmm_staging` folder, created if needed. Raises OSError
    (disk, permissions) for the caller to report as a file error."""
    staging_dir = output_dir / STAGING_SUBDIR_NAME
    staging_dir.mkdir(parents=True, exist_ok=True)
    return staging_dir


def _cleanup_staging_dir_if_empty(staging_dir: Path) -> None:
    """Best-effort tidy-up after a successful commit -- if nothing else
    is currently staged, remove the now-empty staging folder so it
    doesn't linger in every synced playlist folder. Never raises; if
    it's not empty (or already gone), that's fine, it'll be cleaned up
    next time or reported by the integrity check."""
    try:
        staging_dir.rmdir()
    except OSError:
        pass


def _final_path(output_dir: Path, file_name: str, identity_id: str) -> Path:
    """Where a finished download is committed: normally output_dir/<track
    name>.m4a (design doc §3.1). Two different songs can share a title
    (e.g. "Voices" by two artists), and committing the second used to
    overwrite the first, so each sync downloaded one of them again and
    the other went missing (v0.16.37). Now, if that name already holds a
    different track (or an untagged file), " (2)", " (3)"... is added
    instead. A file holding this same track is replaced, as before."""
    candidate = output_dir / file_name
    n = 2
    while candidate.exists() and metadata_tagger.read_track_identity(candidate) != identity_id:
        candidate = output_dir / f"{Path(file_name).stem} ({n}){Path(file_name).suffix}"
        n += 1
    return candidate


def get_playlist_tracks(
    spotify_playlist_id: str,
    skip_details_for: Optional[Collection[str]] = None,
    unavailable: Optional[List[spotify_reader.UnavailableItem]] = None,
    album: bool = False,
) -> List[spotify_reader.Track]:
    """Read the current, live track list for a public Spotify playlist,
    in playlist order, with full details (album, year, cover art).
    skip_details_for (Checklist item 26): Spotify Track IDs to build
    from the listing alone, without album/year/cover art -- normally the
    ones already downloaded. unavailable (v0.16.39, Checklist items
    45 / 46): an optional list that receives the playlist's items Spotify
    gives no details for (see spotify_reader.UnavailableItem).
    album (v0.16.48, Checklist item 41a): the ID is a Spotify album's, read
    with spotify_reader.get_album_tracks() (album order, disc numbers)."""
    reader = spotify_reader.get_album_tracks if album else spotify_reader.get_playlist_tracks
    return reader(spotify_playlist_id, skip_details_for=skip_details_for, unavailable=unavailable)


def get_playlist_tracks_with_details_for(
    spotify_playlist_id: str,
    details_for: Collection[str],
    unavailable: Optional[List[spotify_reader.UnavailableItem]] = None,
    album: bool = False,
) -> List[spotify_reader.Track]:
    """Like get_playlist_tracks(), but only the given Spotify Track IDs
    get the per-track detail lookup (album, year, cover art); the rest
    are built from the listing alone (v0.16.40). For saving exceptions
    and imports, which only tag those tracks. album: as get_playlist_tracks()."""
    reader = spotify_reader.get_album_tracks if album else spotify_reader.get_playlist_tracks
    return reader(spotify_playlist_id, details_only_for=details_for, unavailable=unavailable)


def get_album_info(album_id: str) -> spotify_reader.AlbumInfo:
    """A Spotify album's name, artists, year, cover and track count
    (v0.16.48, Checklist item 41a), e.g. to name its folder. Since
    v0.16.51 a YT Music album ID is read from YT Music (its album_id is
    then the browse ID, even for an OLAK5uy_ playlist ID)."""
    platform = config_manager.id_platform(album_id)
    if platform == config_manager.PLATFORM_YTMUSIC:
        return ytmusic_reader.get_album_info(album_id)
    if platform == config_manager.PLATFORM_APPLE_MUSIC:  # v0.16.53
        return apple_music_reader.get_album_info(album_id)
    if platform == config_manager.PLATFORM_AMAZON_MUSIC:  # v0.16.54
        return amazon_music_reader.get_album_info(album_id)
    return spotify_reader.get_album_info(album_id)


def get_artist_discography(artist_id: str) -> spotify_reader.ArtistInfo:
    """A Spotify artist's name, picture, albums and singles & EPs
    (v0.16.50, Checklist items 41b / 41c); a YT Music artist's since
    v0.16.51."""
    platform = config_manager.id_platform(artist_id)
    if platform == config_manager.PLATFORM_YTMUSIC:
        return ytmusic_reader.get_artist_discography(artist_id)
    if platform == config_manager.PLATFORM_APPLE_MUSIC:  # v0.16.53
        return apple_music_reader.get_artist_discography(artist_id)
    return spotify_reader.get_artist_discography(artist_id)


def get_catalog_tracks(
    entry: dict,
    skip_details_for: Optional[Collection[str]] = None,
    unavailable: Optional[List[spotify_reader.UnavailableItem]] = None,
    details_only_for: Optional[Collection[str]] = None,
) -> List[spotify_reader.Track]:
    """The live songs of a Spotify or (v0.16.53, Checklist item 42c) Apple
    Music playlist / album entry (config_manager.uses_search()), as Tracks
    whose spotify_id is the song's identity, for the tiered YouTube search.
    skip_details_for / details_only_for are identities: the songs that
    don't / only those that need the per-song details (album, year,
    cover). unavailable: Spotify items with no details (stays empty for
    Apple Music)."""
    source_id = config_manager.get_playlist_source_id(entry)
    album = config_manager.is_album(entry)
    platform = config_manager.get_playlist_platform(entry)
    if platform in (config_manager.PLATFORM_APPLE_MUSIC, config_manager.PLATFORM_AMAZON_MUSIC):
        module = apple_music_reader if platform == config_manager.PLATFORM_APPLE_MUSIC else amazon_music_reader
        reader = module.get_album_tracks if album else module.get_playlist_tracks
        return reader(source_id, skip_details_for=skip_details_for, unavailable=unavailable,
                      details_only_for=details_only_for)
    if details_only_for is not None:
        return get_playlist_tracks_with_details_for(source_id, details_only_for, unavailable=unavailable,
                                                    album=album)
    return get_playlist_tracks(source_id, skip_details_for=skip_details_for, unavailable=unavailable,
                               album=album)


def get_video_tracks(
    entry: dict,
    fetch_details: bool = True,
    skip_details_for: Optional[Collection[str]] = None,
    details_only_for: Optional[Collection[str]] = None,
) -> List[youtube_playlist_reader.YouTubeTrack]:
    """The live songs of a YouTube playlist or (v0.16.51, Checklist item
    42a) a YT Music playlist or album entry (config_manager.uses_video_ids()),
    in order. fetch_details=False: only which videos are live (Scan).
    skip_details_for / details_only_for: bare video IDs that don't / only
    those that need the per-song details (YouTube: the per-video fetch; YT
    Music: album years and covers, audio-only versions). details_only_for
    is a YT Music refinement; a YouTube playlist then reads no details,
    as the Exceptions pickers always did."""
    source_id = config_manager.get_playlist_source_id(entry)
    if config_manager.get_playlist_platform(entry) == config_manager.PLATFORM_YTMUSIC:
        reader = (ytmusic_reader.get_album_tracks if config_manager.is_album(entry)
                  else ytmusic_reader.get_playlist_tracks)
        return reader(source_id, fetch_details=fetch_details, skip_details_for=skip_details_for,
                      details_only_for=details_only_for)
    if details_only_for is not None:
        fetch_details = False
    return get_youtube_playlist_tracks(source_id, fetch_details=fetch_details,
                                       skip_details_for=skip_details_for)


def get_soundcloud_tracks(entry: dict, **_details) -> List[soundcloud_reader.SoundCloudTrack]:
    """The live songs of a SoundCloud playlist or album entry, in order
    (v0.16.52, Checklist item 42b). Every song's details are read (cheap:
    one call per 50 songs), so the details arguments other readers take
    are accepted and ignored."""
    return soundcloud_reader.get_set_tracks(config_manager.get_playlist_source_id(entry))


def get_soundcloud_set_info(set_input: str) -> soundcloud_reader.SetInfo:
    """A SoundCloud set's title, uploader, whether it's an album, year and
    track count; follows a short link (v0.16.52). For naming it and
    deciding its kind before it's added."""
    return soundcloud_reader.get_set_info(set_input)


def get_youtube_playlist_tracks(
    youtube_playlist_id: str,
    fetch_details: bool = True,
    skip_details_for: Optional[Collection[str]] = None,
) -> List[youtube_playlist_reader.YouTubeTrack]:
    """
    Read the current, live video list for a YouTube-native playlist, in
    playlist order (Checklist item 6, v0.15.0). See
    youtube_playlist_reader.py for what metadata is and isn't available
    for these tracks compared to a Spotify one.

    fetch_details=False skips the expensive per-video detail fetch --
    used by read-only Scan, which only needs to know which videos are
    live, not their full tag data. skip_details_for (Checklist item 26)
    skips it just for the given bare video IDs -- normally the ones
    already downloaded.
    """
    return youtube_playlist_reader.get_playlist_tracks(
        youtube_playlist_id, fetch_details=fetch_details, skip_details_for=skip_details_for
    )


def acquire_track(
    track: "spotify_reader.Track",
    output_dir: Path,
    total_tracks: Optional[int] = None,
    override_youtube_id: Optional[str] = None,
) -> AcquisitionResult:
    """
    Find the best YouTube match for a single Spotify-sourced track
    (tiered search), download it, and tag it with the project's
    metadata field set.

    total_tracks is the playlist's total track count, used for the
    Track # field's "N of total" pairing -- fine to omit.

    override_youtube_id: if set (e.g. from exceptions_manager), skips
    the normal tiered search entirely and downloads this specific video
    instead. Used to manually fix a track that got a bad automatic
    match, or one the search couldn't find at all.

    The download and tag are both performed in a `.mmm_staging`
    subfolder of output_dir first, and only moved into output_dir
    itself once both steps have been attempted -- see the module
    docstring for why (Checklist item 3, crash safety).

    Returns an AcquisitionResult. If no match was found in any tier
    (and no override was given), or the download itself failed,
    file_path will be None and failure_reason will hold a specific,
    human-readable explanation (e.g. "HTTP Error 403: Forbidden", or a
    summary of why every search tier came up empty) -- suitable for
    showing directly in the console or a download log, not just
    logs/error_log.txt. failure_kind (v0.16.27) says which kind of
    failure it was (track_failures): only NO_MATCH is a case for the
    Exceptions system (exceptions_manager.add_exception) by itself; most
    other kinds are temporary and the next sync tries again. A refused
    download is already retried inside downloader.download_audio().
    tagged is False whenever file_path is None, and
    also if the download succeeded but tagging itself failed for some
    reason (rare -- the file still exists either way, just untagged).
    """
    output_dir = Path(output_dir)

    if override_youtube_id:
        match = youtube_matcher.MatchResult(
            video_id=override_youtube_id,
            title=track.name,
            tier="exception",
            url=f"https://www.youtube.com/watch?v={override_youtube_id}",
        )
    else:
        if getattr(track, "match_without_length", False) and not track.duration_seconds:
            # v0.16.54 (Checklist item 42d): an Amazon Music song with no known
            # length -- only a strict official-audio match (Michael's choice).
            match, failure_reason, failure_kind = youtube_matcher.find_strict_match(track)
        else:
            match, failure_reason, failure_kind = youtube_matcher.find_best_match(track)
        if match is None:
            return AcquisitionResult(
                track=track, match=None, file_path=None, tagged=False,
                has_lyrics=False, failure_reason=failure_reason, failure_kind=failure_kind,
            )

    try:
        staging_dir = _make_staging_dir(output_dir)
    except OSError as e:
        logger.log_error(f"Couldn't create the staging folder in {output_dir}: {e}")
        return AcquisitionResult(
            track=track, match=match, file_path=None, tagged=False, has_lyrics=False,
            failure_reason=f"couldn't create a folder to download into: {e}",
            failure_kind=track_failures.FILE_ERROR,
        )

    filename_base = track.name
    staged_path, download_failure_reason, download_failure_kind = downloader.download_audio(
        match.url, staging_dir, filename_base
    )

    if staged_path is None:
        logger.log_error(
            f"Matched '{track.primary_artist} - {track.name}' to "
            f"{match.url} (tier: {match.tier}) but the download failed: {download_failure_reason}"
        )
        return AcquisitionResult(
            track=track, match=match, file_path=None, tagged=False,
            has_lyrics=False, failure_reason=download_failure_reason, failure_kind=download_failure_kind,
        )

    lyrics = lyrics_provider.get_lyrics(track)

    tagged = metadata_tagger.tag_file(
        staged_path, track, identity_id=track.spotify_id, youtube_id=match.video_id,
        total_tracks=total_tracks, lyrics=lyrics,
    )
    if not tagged:
        logger.log_error(
            f"Downloaded '{track.primary_artist} - {track.name}' to "
            f"{staged_path} (staged) but tagging failed"
        )

    # Commit: this is the crash-safety boundary (Checklist item 3). Up
    # to this point, nothing in the real, live playlist folder has been
    # touched at all -- only the disposable staging copy. Moving it
    # into place with os.replace() is a single atomic filesystem
    # operation (staging_dir is a subfolder of output_dir, so this is
    # always a same-filesystem move), so the real folder only ever
    # sees the fully-downloaded-and-tag-attempted file appear whole,
    # never partially. _final_path() never lets it land on another
    # track's file (v0.16.37).
    final_path = _final_path(output_dir, staged_path.name, track.spotify_id)
    try:
        os.replace(staged_path, final_path)
    except OSError as e:
        logger.log_error(
            f"Downloaded and tagged '{track.primary_artist} - {track.name}' but "
            f"failed to move it out of staging into {output_dir}: {e}"
        )
        return AcquisitionResult(
            track=track, match=match, file_path=None, tagged=False,
            has_lyrics=bool(lyrics),
            failure_reason=f"downloaded and tagged, but couldn't move the file into place: {e}",
            failure_kind=track_failures.FILE_ERROR,
        )

    _cleanup_staging_dir_if_empty(staging_dir)

    return AcquisitionResult(
        track=track, match=match, file_path=final_path, tagged=tagged,
        has_lyrics=bool(lyrics), failure_reason=None if tagged else "download succeeded but tagging failed",
    )


def import_audio_file(
    track,
    source_path: Path,
    output_dir: Path,
    identity_id: str,
    total_tracks: Optional[int] = None,
) -> AcquisitionResult:
    """
    Put the user's own audio file in the playlist as this track
    (v0.16.40, Checklist item 47). `track` is a spotify_reader.Track or
    youtube_playlist_reader.YouTubeTrack giving the tags (for a song
    Spotify gives no details for, one built from what the user typed);
    identity_id is its Comment identity, as a download's would be.

    An .m4a is copied as it is; anything else is converted to AAC with
    ffmpeg (downloader.convert_to_m4a()). Imports are always .m4a: the
    download file format setting (v0.16.45) doesn't apply to them. Then it is tagged like a
    download (lyrics looked up too; Grouping / Composer "Own file:
    <name>"; the file's own cover kept if Spotify has none) and
    committed with the same staging / atomic move as a download, under
    the track's name (" (2)"... on a clash, _final_path()). Unlike a
    download, a file that couldn't be tagged is NOT committed: without
    its identity tag the diff would never recognise it.

    The source file is only read, never moved or changed. Returns an
    AcquisitionResult with match None; on failure file_path is None and
    failure_reason says why (failure_kind FILE_ERROR).
    """
    source = Path(source_path)
    output_dir = Path(output_dir)

    def failed(reason: str) -> AcquisitionResult:
        logger.log_error(f"Couldn't import '{source}' as '{track.name}': {reason}")
        return AcquisitionResult(
            track=track, match=None, file_path=None, tagged=False, has_lyrics=False,
            failure_reason=reason, failure_kind=track_failures.FILE_ERROR,
        )

    if not source.is_file():
        return failed("the file can't be found (was it moved or deleted?)")

    try:
        staging_dir = _make_staging_dir(output_dir)
    except OSError as e:
        return failed(f"couldn't create a folder to work in: {e}")

    staged_path = staging_dir / f"{downloader.sanitize_filename(track.name) or 'Imported song'}.m4a"
    try:
        if staged_path.exists():
            staged_path.unlink()  # a leftover from an interrupted import
    except OSError as e:
        return failed(f"couldn't clear an old staged file: {e}")

    if source.suffix.lower() == ".m4a":
        try:
            shutil.copyfile(source, staged_path)
        except OSError as e:
            return failed(f"couldn't copy the file: {e}")
    else:
        reason = downloader.convert_to_m4a(source, staged_path)
        if reason:
            _discard_staged(staged_path, staging_dir)
            return failed(reason)

    try:
        if not (MP4(staged_path).info.length or 0) > 0:
            raise ValueError("it has no audio")
    except Exception as e:
        _discard_staged(staged_path, staging_dir)
        return failed(f"the result isn't a playable .m4a file ({e})")

    lyrics = lyrics_provider.get_lyrics(track)
    tagged = metadata_tagger.tag_file(
        staged_path, track, identity_id=identity_id,
        youtube_id=metadata_tagger.build_own_file_marker(source.name),
        total_tracks=total_tracks, lyrics=lyrics,
        fallback_cover=metadata_tagger.read_embedded_cover(source),
    )
    if not tagged:
        _discard_staged(staged_path, staging_dir)
        return failed("the file couldn't be tagged")

    final_path = _final_path(output_dir, staged_path.name, identity_id)
    try:
        os.replace(staged_path, final_path)
    except OSError as e:
        _discard_staged(staged_path, staging_dir)
        return failed(f"couldn't move the file into the playlist folder: {e}")

    _cleanup_staging_dir_if_empty(staging_dir)
    logger.log_event(f"Imported '{source}' as '{final_path.name}' ({identity_id})")
    return AcquisitionResult(track=track, match=None, file_path=final_path, tagged=True,
                             has_lyrics=bool(lyrics))


def _discard_staged(staged_path: Path, staging_dir: Path) -> None:
    """Remove a failed import's staged file (never raises)."""
    try:
        staged_path.unlink()
    except OSError:
        pass
    _cleanup_staging_dir_if_empty(staging_dir)


def acquire_youtube_native_track(
    track: "youtube_playlist_reader.YouTubeTrack",
    output_dir: Path,
    total_tracks: Optional[int] = None,
    override_video_id: Optional[str] = None,
) -> AcquisitionResult:
    """
    Download and tag a single video from a YouTube-native playlist
    (Checklist item 6, v0.15.0). Per design Q3, none of the search
    tiers in youtube_matcher.py run here -- the video to download is
    already known (it's the playlist entry itself), so this always
    downloads it directly.

    override_video_id: if set (via exceptions_manager, same mechanism
    as acquire_track()'s override_youtube_id), downloads THIS video
    instead of the one currently in the live playlist -- e.g. if the
    original upload was taken down or Michael wants to substitute a
    different version. Crucially, the file's identity/Comment tag
    still records the ORIGINAL playlist video's ID (via
    metadata_tagger.build_youtube_identity(track.video_id)), not the
    override's -- exactly mirroring how acquire_track()'s override
    always keeps the Spotify Track ID as the identity regardless of
    which YouTube video actually got downloaded. This is what lets the
    Library Diff Mechanism keep recognizing this file as "that live
    playlist entry, already downloaded" on every future sync, even
    though the override is a different video than the ID reflects.
    The override's own video ID is still recorded in Grouping/Composer,
    same as always, for tracing.

    The download and tag are both performed in a `.mmm_staging`
    subfolder of output_dir first, and only moved into output_dir
    itself once both steps have been attempted -- identical
    crash-safety shape to acquire_track() (Checklist item 3).

    Returns an AcquisitionResult, same shape as acquire_track()'s.
    match.tier is "youtube_native" for a normal download, or
    "exception" when override_video_id was used.
    """
    output_dir = Path(output_dir)
    identity_id = metadata_tagger.build_youtube_identity(track.video_id)

    download_video_id = override_video_id or track.video_id
    tier = "exception" if override_video_id else "youtube_native"
    if not override_video_id and getattr(track, "wants_audio_version", False):
        # v0.16.51 (Checklist item 42a): a YT Music album song listed as a
        # video -- its audio-only version if there is one (Michael's choice).
        audio_id = track.audio_video_id
        if not audio_id:
            found, search_error = youtube_matcher.find_audio_version(track)
            if search_error:
                kind = track_failures.classify_search_error(search_error)
                reason = (f"YouTube Music search failed ({search_error}), so the song's audio-only "
                          f"version couldn't be looked for")
                logger.log_error(f"YT Music album song '{track.title}': {reason}")
                return AcquisitionResult(track=track, match=None, file_path=None, tagged=False,
                                         has_lyrics=False, failure_reason=reason, failure_kind=kind)
            audio_id = found.video_id if found else None
        if audio_id and audio_id != track.video_id:
            download_video_id, tier = audio_id, "ytmusic_atv"
            logger.log_event(f"YT Music album song '{track.title}': downloading its audio-only version "
                             f"{audio_id} instead of the listed video {track.video_id}")
        else:
            logger.log_event(f"YT Music album song '{track.title}': no audio-only version found, "
                             f"downloading the listed video {track.video_id}")
    match = youtube_matcher.MatchResult(
        video_id=download_video_id,
        title=track.title,
        tier=tier,
        url=f"https://www.youtube.com/watch?v={download_video_id}",
    )

    try:
        staging_dir = _make_staging_dir(output_dir)
    except OSError as e:
        logger.log_error(f"Couldn't create the staging folder in {output_dir}: {e}")
        return AcquisitionResult(
            track=track, match=match, file_path=None, tagged=False, has_lyrics=False,
            failure_reason=f"couldn't create a folder to download into: {e}",
            failure_kind=track_failures.FILE_ERROR,
        )

    filename_base = track.name
    staged_path, download_failure_reason, download_failure_kind = downloader.download_audio(
        match.url, staging_dir, filename_base
    )

    if staged_path is None:
        logger.log_error(
            f"YouTube-native track '{track.title}' ({match.url}, tier: {match.tier}) "
            f"failed to download: {download_failure_reason}"
        )
        return AcquisitionResult(
            track=track, match=match, file_path=None, tagged=False,
            has_lyrics=False, failure_reason=download_failure_reason, failure_kind=download_failure_kind,
        )

    lyrics = lyrics_provider.get_lyrics(track)

    tagged = metadata_tagger.tag_file(
        staged_path, track, identity_id=identity_id, youtube_id=match.video_id,
        total_tracks=total_tracks, lyrics=lyrics,
    )
    if not tagged:
        logger.log_error(
            f"Downloaded YouTube-native track '{track.title}' to {staged_path} "
            f"(staged) but tagging failed"
        )

    final_path = _final_path(output_dir, staged_path.name, identity_id)
    try:
        os.replace(staged_path, final_path)
    except OSError as e:
        logger.log_error(
            f"Downloaded and tagged YouTube-native track '{track.title}' but failed "
            f"to move it out of staging into {output_dir}: {e}"
        )
        return AcquisitionResult(
            track=track, match=match, file_path=None, tagged=False,
            has_lyrics=bool(lyrics),
            failure_reason=f"downloaded and tagged, but couldn't move the file into place: {e}",
            failure_kind=track_failures.FILE_ERROR,
        )

    _cleanup_staging_dir_if_empty(staging_dir)

    return AcquisitionResult(
        track=track, match=match, file_path=final_path, tagged=tagged,
        has_lyrics=bool(lyrics), failure_reason=None if tagged else "download succeeded but tagging failed",
    )


def _youtube_fallback(track, why: str):
    """(match, reason, kind) for a SoundCloud song found on YouTube
    instead (v0.16.52): the usual tiered search (§3.4), with its real
    length. A failure's reason says why YouTube was searched at all."""
    match, reason, kind = youtube_matcher.find_best_match(track)
    if match is None:
        reason = f"{why}, and YouTube had no match: {reason}"
    else:
        logger.log_event(f"SoundCloud song '{track.primary_artist} - {track.name}': {why}, so it was "
                         f"found on YouTube instead ({match.url}, tier: {match.tier})")
    return match, reason, kind


def acquire_soundcloud_track(
    track: "soundcloud_reader.SoundCloudTrack",
    output_dir: Path,
    total_tracks: Optional[int] = None,
    override_video_id: Optional[str] = None,
) -> AcquisitionResult:
    """
    Download and tag one song of a SoundCloud set (v0.16.52, Checklist
    item 42b). In order:
      - an exception link (a YouTube video) is downloaded if there is one;
      - a song SoundCloud only previews or blocks (track.preview_only) is
        found on YouTube with the tiered search (Michael's choice);
      - otherwise the SoundCloud upload itself is downloaded (yt-dlp, the
        download settings as for YouTube). If SoundCloud refuses it as
        DRM-protected, it too is found on YouTube; any other failure is
        reported as usual (a refused download is retried inside
        downloader.download_audio()).
    The identity is "sc:<track id>" in every case. Grouping / Composer
    hold "SoundCloud: <track id>" for a SoundCloud download, or the
    YouTube video ID. Same staging / atomic commit as every download.
    match.tier is "soundcloud", "exception", or the YouTube search's tier.
    """
    output_dir = Path(output_dir)
    identity_id = metadata_tagger.build_soundcloud_identity(track.soundcloud_id)

    def failed(match, reason, kind):
        return AcquisitionResult(track=track, match=match, file_path=None, tagged=False,
                                 has_lyrics=False, failure_reason=reason, failure_kind=kind)

    try:
        staging_dir = _make_staging_dir(output_dir)
    except OSError as e:
        logger.log_error(f"Couldn't create the staging folder in {output_dir}: {e}")
        return failed(None, f"couldn't create a folder to download into: {e}", track_failures.FILE_ERROR)

    if override_video_id:
        match = youtube_matcher.MatchResult(
            video_id=override_video_id, title=track.name, tier="exception",
            url=f"https://www.youtube.com/watch?v={override_video_id}")
    elif track.preview_only:
        why = ("SoundCloud only plays a 30-second preview of it" if track.policy == "SNIP"
               else "SoundCloud won't play it here")
        match, reason, kind = _youtube_fallback(track, why)
        if match is None:
            return failed(None, reason, kind)
    else:
        match = youtube_matcher.MatchResult(
            video_id=track.soundcloud_id, title=track.name, tier="soundcloud", url=track.permalink_url)

    staged_path, reason, kind = downloader.download_audio(match.url, staging_dir, track.name)
    if staged_path is None and match.tier == "soundcloud" and "drm" in (reason or "").lower():
        match, fallback_reason, fallback_kind = _youtube_fallback(track, "SoundCloud refused it as DRM-protected")
        if match is None:
            return failed(None, fallback_reason, fallback_kind)
        staged_path, reason, kind = downloader.download_audio(match.url, staging_dir, track.name)
    if staged_path is None:
        logger.log_error(f"SoundCloud song '{track.primary_artist} - {track.name}' ({match.url}, tier: "
                         f"{match.tier}) failed to download: {reason}")
        return failed(match, reason, kind)

    lyrics = lyrics_provider.get_lyrics(track)
    source = (metadata_tagger.build_soundcloud_marker(track.soundcloud_id) if match.tier == "soundcloud"
              else match.video_id)
    tagged = metadata_tagger.tag_file(staged_path, track, identity_id=identity_id, youtube_id=source,
                                      total_tracks=total_tracks, lyrics=lyrics)
    if not tagged:
        logger.log_error(f"Downloaded SoundCloud song '{track.name}' to {staged_path} (staged) but tagging failed")

    final_path = _final_path(output_dir, staged_path.name, identity_id)
    try:
        os.replace(staged_path, final_path)
    except OSError as e:
        logger.log_error(f"Downloaded and tagged SoundCloud song '{track.name}' but failed to move it out "
                         f"of staging into {output_dir}: {e}")
        return AcquisitionResult(
            track=track, match=match, file_path=None, tagged=False, has_lyrics=bool(lyrics),
            failure_reason=f"downloaded and tagged, but couldn't move the file into place: {e}",
            failure_kind=track_failures.FILE_ERROR)

    _cleanup_staging_dir_if_empty(staging_dir)
    return AcquisitionResult(
        track=track, match=match, file_path=final_path, tagged=tagged, has_lyrics=bool(lyrics),
        failure_reason=None if tagged else "download succeeded but tagging failed")