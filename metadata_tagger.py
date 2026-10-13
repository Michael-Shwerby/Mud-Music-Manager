"""
Mud Music Manager - Metadata Tagger

Burns the metadata field set from the design doc (Section 3.1) into a
downloaded m4a file using mutagen -- directly, replacing whatever
tags (if any) yt-dlp left on the file by default.

Fields written:
  Title, Artist, Album, Year, Track # (current live playlist position,
  Spotify or YouTube), Cover art, Comment (the track's stable diff/
  identity key -- see below), Grouping (the YouTube video ID actually
  downloaded, for tracing a bad match via the Exceptions system) -- as
  of v0.13.0, also duplicated into the Composer field (see "Checklist
  item 5" note below).

Lyrics are sourced via syncedlyrics (see lyrics_provider.py) for both
Spotify- and YouTube-native tracks.

Checklist item 5 (YouTube ID -> Windows 11-accessible metadata field):
  Confirmed by Michael that Windows 11's Explorer Properties panel
  (Details tab, and as an addable column) does not surface the
  Grouping (`©grp`) atom at all, even though mutagen itself reads it
  back fine -- so the traced YouTube ID was effectively invisible
  outside the app. Rather than moving the ID (which would break
  tracing on already-downloaded, already-tagged files), it's now also
  duplicated into the Composer field (`©wrt`), which Windows 11 DOES
  expose for m4a files. This deliberately repurposes Composer for
  tracing rather than an actual composer credit -- a personal-use
  trade-off, not a mistake, and one worth revisiting if a different
  Windows-visible field (e.g. Subtitle) is preferred instead.
  `read_youtube_id()` checks the Composer field first (the new
  Windows-visible source of truth going forward) and falls back to
  Grouping for files tagged before this change, so nothing already
  downloaded needs re-tagging.

Checklist item 6 (YouTube-native playlist sync, v0.15.0):
  Comment (`©cmt`) used to always hold the Spotify Track ID, so
  tag_file() derived it internally from track.spotify_id. A
  YouTube-native track has no Spotify ID -- its identity key is its
  own YouTube video ID, written as "yt:<videoId>" (see
  YOUTUBE_IDENTITY_PREFIX / build_youtube_identity()) so it's
  unambiguous at a glance from a bare Spotify ID, including to a human
  reading the tag directly and to Check Integrity's tag-reading.
  tag_file() now takes this value as an explicit `identity_id`
  parameter instead of reading track.spotify_id itself, decoupling the
  Comment field's meaning from any one platform's Track type -- the
  Library Sync/Diff Engine (library_sync.py) treats Comment as an
  opaque identity string either way, so the diff algorithm itself
  (design doc §3.3) needed no change at all, just a caller that builds
  the right string per platform. Existing Spotify-tagged files are
  completely unaffected: their Comment value is still the bare Spotify
  Track ID, unprefixed, exactly as before -- nothing needs re-tagging.
  `read_spotify_id()` is kept as a backward-compatible alias for
  `read_track_identity()`, which is the new, platform-neutral name
  used going forward by library_sync.py.

Own files (v0.16.40, Checklist item 47):
  A song imported from the user's own audio file is tagged exactly like
  a download (same Comment identity, so the diff treats it the same),
  but Grouping / Composer hold "Own file: <original file name>" instead
  of a YouTube video ID (build_own_file_marker() / own_file_name()).
  If Spotify has no cover art for it, the cover embedded in the user's
  file is kept (tag_file()'s fallback_cover, read_embedded_cover()).
  read_source_tags() reads the user's file's own title / artist / album
  / year, to pre-fill the import form.

Other file formats (v0.16.45, Checklist item 39): downloads can also be
.mp3, .ogg (Vorbis), .flac or .wav (Settings -> Download quality). The
same field set is written in each format's own tags; the m4a path is
unchanged:

  Field        m4a     mp3 / wav (ID3v2.3)   flac / ogg (Vorbis comment)
  Title        ©nam    TIT2                  TITLE
  Artist       ©ART    TPE1                  ARTIST
  Album        ©alb    TALB                  ALBUM
  Year         ©day    TDRC (TYER in 2.3)    DATE
  Track #      trkn    TRCK "n/total"        TRACKNUMBER + TRACKTOTAL
  Cover        covr    APIC (front cover)    FLAC picture / METADATA_BLOCK_PICTURE
  Lyrics       ©lyr    USLT                  LYRICS
  Comment      ©cmt    COMM (no description) COMMENT
  Grouping     ©grp    TIT1                  GROUPING
  Composer     ©wrt    TCOM                  COMPOSER

Disc # (v0.16.48, Checklist item 41a): an album track (a Track with a
disc_number) also gets Disc # "n of total discs": m4a `disk`, ID3 TPOS
"n/total", Vorbis DISCNUMBER + DISCTOTAL. Playlist tracks have no disc
number and get none, as before. read_tags() reads it back (FileTags.
disc_number) so the Edit page can list an album's discs in order.

Everything that reads a playlist folder goes through audio_files() and
read_tags() / read_track_identity() here, so the library diff (design
doc §3.3) reads every format the same way and a folder can hold files of
several formats (e.g. after the format setting changed). For a non-m4a
file, the Comment only counts as this app's identity if it has the shape
of one (a Spotify Track ID or "yt:<videoId>", _looks_like_identity()):
an mp3 / FLAC from elsewhere often carries a comment of its own, and the
diff removes a file whose identity isn't live, so a stray comment must
never look like a track of the playlist. The m4a rule is unchanged.

SoundCloud (v0.16.52, Checklist item 42b): a SoundCloud song's identity is
"sc:<track id>" (build_soundcloud_identity()), which also counts as an
identity shape. Grouping / Composer say "SoundCloud: <track id>" when the
song came from SoundCloud itself (build_soundcloud_marker()), or hold the
YouTube video ID when SoundCloud only had a preview and it came from
YouTube.

Apple Music (v0.16.53, Checklist item 42c): an Apple Music song's identity
is "am:<Apple song ID>" (apple_music_reader.identity()), also an identity
shape. It is tagged like a Spotify song (found on YouTube). v0.16.54
(Checklist item 42d): an Amazon Music song's is "amz:<song ASIN>".

Cover art format (v0.17.3, Checklist item 54): with Settings -> Download
quality -> Cover art on "PNG" (the default), the cover is first asked for
as a PNG from servers that render one from their own source picture
(png_cover_url(): Apple Music, YT Music, Deezer; Amazon is tried). Only a
real PNG (by its signature) is used. v0.17.4: otherwise the cover link as
it was is downloaded and, if it isn't a PNG, converted to one with ffmpeg
(_convert_cover_to_png(); lossless, from the decoded picture), so every
cover is a PNG (Michael, 2026-10-11). Without ffmpeg, or if it fails, the
original image is embedded as it is. With "JPEG" the image's bytes are
embedded exactly as sent.
"""

import base64
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import mutagen
import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, TALB, TCOM, TDRC, TIT1, TIT2, TPE1, TPOS, TRCK, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

import download_settings
import logger
from spotify_reader import Track

# Suffix used for the temporary copy update_track_number() stages its
# edit in before swapping it into place. Kept as a module-level
# constant (rather than buried in the function) so library_sync.py's
# corrupted/mismatched-data scan can recognize and flag any leftover
# copy from an interrupted renumber using the exact same string.
RETAG_TEMP_SUFFIX = ".retag-tmp"

# YouTube video ID storage (Checklist item 5). The Grouping atom is the
# original, long-standing location (kept for backward compatibility --
# every file tagged before v0.13.0 only has this one). The Composer
# atom is the new, Windows 11 Explorer-visible duplicate written
# starting in v0.13.0. Kept as module-level constants (matching the
# RETAG_TEMP_SUFFIX convention above) so both the writer and reader
# stay in sync if this ever needs to change again.
LEGACY_YOUTUBE_ID_ATOM = "\xa9grp"  # Grouping -- not visible in Windows 11 Explorer
WINDOWS_VISIBLE_YOUTUBE_ID_ATOM = "\xa9wrt"  # Composer -- IS visible in Windows 11 Explorer

# Prefix marking a Comment-tag identity value as a YouTube video ID
# rather than a bare Spotify Track ID (Checklist item 6, v0.15.0). See
# build_youtube_identity() and the module docstring above.
YOUTUBE_IDENTITY_PREFIX = "yt:"

# A SoundCloud song's identity: "sc:<track id>" (v0.16.52, Checklist item
# 42b). The track ID is SoundCloud's own number, which never changes.
SOUNDCLOUD_IDENTITY_PREFIX = "sc:"

# Written to Grouping / Composer in place of a YouTube video ID for a
# song imported from the user's own file (v0.16.40, Checklist item 47).
OWN_FILE_PREFIX = "Own file: "

# Written to Grouping / Composer for a song downloaded from SoundCloud
# itself (v0.16.52): "SoundCloud: <track id>". When SoundCloud only had a
# preview and the song came from YouTube, the YouTube video ID is written
# as usual.
SOUNDCLOUD_SOURCE_PREFIX = "SoundCloud: "

# The file extensions a playlist folder's songs can have (v0.16.45,
# Checklist item 39) -> the tag family each one uses.
AUDIO_FORMATS = {".m4a": "mp4", ".mp3": "id3", ".wav": "id3", ".flac": "vorbis", ".ogg": "vorbis"}
AUDIO_EXTENSIONS = tuple(AUDIO_FORMATS)

# What an identity written by this app looks like (see module docstring).
_IDENTITY_RE = re.compile(r"^(?:[0-9A-Za-z]{22}|" + re.escape(YOUTUBE_IDENTITY_PREFIX) + r"[0-9A-Za-z_-]{11}|"
                          + re.escape(SOUNDCLOUD_IDENTITY_PREFIX) + r"[0-9]{1,20}|"  # sc: since v0.16.52
                          + r"am:[0-9]{1,20}|"  # Apple Music songs, "am:<song id>", since v0.16.53
                          + r"amz:[A-Z0-9]{10})$")  # Amazon Music songs, "amz:<ASIN>", since v0.16.54


def is_audio_file(path) -> bool:
    """Whether a file has one of the song extensions (any case)."""
    return Path(path).suffix.lower() in AUDIO_FORMATS


def audio_files(folder) -> List[Path]:
    """The song files directly inside `folder` (v0.16.45: every format in
    AUDIO_EXTENSIONS; it was `folder.glob("*.m4a")`). Non-recursive, so
    the `.mmm_staging` subfolder is never included, and a renumber's
    `*.retag-tmp` copy isn't a song. Sorted by name."""
    try:
        return sorted((p for p in Path(folder).iterdir() if p.is_file() and is_audio_file(p)),
                      key=lambda p: p.name.lower())
    except OSError:
        return []


def _looks_like_identity(value: Optional[str]) -> bool:
    return bool(value) and bool(_IDENTITY_RE.match(value))


def _family(path) -> str:
    family = AUDIO_FORMATS.get(Path(path).suffix.lower())
    if family is None:
        raise ValueError(f"not a supported song format ({Path(path).suffix or 'no extension'})")
    return family


def _open(path, like=None):
    """Open a song file with mutagen for its format, with an empty tag
    block added if it had none. `like`: the path whose extension decides
    the format (a renumber's temp copy is opened like its original).
    Raises on an unreadable file."""
    ext = Path(like or path).suffix.lower()
    _family(like or path)
    if ext == ".m4a":
        return MP4(path)
    if ext == ".mp3":
        audio = MP3(path)
    elif ext == ".wav":
        audio = WAVE(path)
    elif ext == ".flac":
        audio = FLAC(path)
    else:  # .ogg: Vorbis (what downloads are), or Opus
        try:
            audio = OggVorbis(path)
        except Exception:
            audio = OggOpus(path)
    if audio.tags is None:
        audio.add_tags()
    return audio


def _save(audio, family: str) -> None:
    if family == "id3":
        audio.save(v2_version=3)  # ID3v2.3: what Windows Explorer reads best
    else:
        audio.save()


def _first_text(values) -> Optional[str]:
    if not values:
        return None
    value = values[0]
    text = getattr(value, "text", None)  # an ID3 frame
    if text is not None:
        value = text[0] if text else ""
    value = str(value).strip()
    return value or None


def _id3_comment(tags) -> Optional[str]:
    """The COMM frame with no description (the one tag_file() writes)."""
    for frame in tags.getall("COMM"):
        if not frame.desc and frame.text:
            return str(frame.text[0]).strip() or None
    return None


def _parse_track_number(value) -> Optional[int]:
    try:
        return int(str(value).split("/")[0].strip())
    except (TypeError, ValueError):
        return None


def _cover_mime(cover) -> str:
    return "image/png" if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"


def _cover_of(audio) -> Optional[MP4Cover]:
    """The embedded front cover of an opened file, as an MP4Cover."""
    if isinstance(audio, MP4):
        covers = audio.get("covr")
        return covers[0] if covers else None
    pictures = getattr(audio, "pictures", None)  # FLAC
    if pictures:
        return _cover_from_bytes(pictures[0].data, pictures[0].mime)
    tags = audio.tags
    if tags is None:
        return None
    if hasattr(tags, "getall"):  # ID3
        frames = tags.getall("APIC")
        if frames:
            return _cover_from_bytes(frames[0].data, frames[0].mime)
        return None
    blocks = tags.get("metadata_block_picture") if hasattr(tags, "get") else None  # Ogg
    if blocks:
        picture = Picture(base64.b64decode(blocks[0]))
        return _cover_from_bytes(picture.data, picture.mime)
    return None


def build_own_file_marker(source_name: str) -> str:
    """Grouping / Composer value of an imported song: "Own file: <name>"."""
    return f"{OWN_FILE_PREFIX}{source_name}"


def own_file_name(value: Optional[str]) -> Optional[str]:
    """The original file name if a Grouping / Composer value marks an
    imported song (None for a YouTube ID or nothing)."""
    if value and value.startswith(OWN_FILE_PREFIX):
        return value[len(OWN_FILE_PREFIX):] or "?"
    return None


def build_youtube_identity(video_id: str) -> str:
    """
    The Comment-tag identity value for a YouTube-native track --
    "yt:<videoId>". Centralized here (rather than inlined at each call
    site) so the exact prefix format can never drift between the
    writer (metadata_tagger.tag_file(), via the identity_id a caller
    builds with this) and anything that needs to compute the same
    value to compare against it (library_sync.py's live-playlist diff).
    """
    return f"{YOUTUBE_IDENTITY_PREFIX}{video_id}"


def build_soundcloud_identity(track_id) -> str:
    """The Comment-tag identity of a SoundCloud song: "sc:<track id>"
    (v0.16.52, Checklist item 42b)."""
    return f"{SOUNDCLOUD_IDENTITY_PREFIX}{track_id}"


def build_soundcloud_marker(track_id) -> str:
    """Grouping / Composer of a song downloaded from SoundCloud itself:
    "SoundCloud: <track id>" (v0.16.52)."""
    return f"{SOUNDCLOUD_SOURCE_PREFIX}{track_id}"


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# v0.17.3 (Checklist item 54): cover links whose server renders a PNG from
# its own source picture when asked (checked 2026-10-11, except Amazon,
# which is tried and checked per download). Spotify, SoundCloud and YouTube
# thumbnails only serve JPEG, so they have no rule.
_PNG_COVER_RULES = (
    # Apple Music: ".../600x600bb.jpg" -> ".../600x600bb.png"
    (re.compile(r"^(https?://[^/]*mzstatic\.com/.+/\d+x\d+bb)\.(?:jpe?g|webp)$", re.I), r"\1.png"),
    # YT Music: "...=w544-h544-l90-rj" -> "...=w544-h544-rp"
    (re.compile(r"^(https?://[^/]*(?:googleusercontent|ggpht)\.com/.+=w\d+-h\d+)[^/=]*$", re.I), r"\1-rp"),
    # Deezer: ".../1000x1000-000000-80-0-0.jpg" -> ".png"
    (re.compile(r"^(https?://[^/]*(?:dzcdn\.net|deezer\.com)/images/.+)\.jpe?g$", re.I), r"\1.png"),
    # Amazon: "..._SY600_.jpg" -> ".png" (not confirmed; falls back if not a PNG)
    (re.compile(r"^(https?://[^/]*(?:media-amazon|images-amazon)\.com/images/.+)\.jpe?g$", re.I), r"\1.png"),
)


def png_cover_url(cover_url: Optional[str]) -> Optional[str]:
    """The PNG form of a cover link, or None when its server has none."""
    for pattern, replacement in _PNG_COVER_RULES:
        if cover_url and pattern.match(cover_url):
            return pattern.sub(replacement, cover_url)
    return None


def _download_png_cover(cover_url: str) -> Optional[MP4Cover]:
    """The cover as a PNG from its server (v0.17.3), or None -- quietly,
    since the caller then uses the cover link as it was."""
    png_url = png_cover_url(cover_url)
    if not png_url:
        return None
    try:
        response = requests.get(png_url, timeout=10)
        response.raise_for_status()
    except Exception as e:
        logger.log_event(f"No PNG cover at {png_url} ({e}); using {cover_url}")
        return None
    if response.content[:8] != _PNG_SIGNATURE:
        logger.log_event(f"{png_url} didn't send a PNG; using {cover_url}")
        return None
    return MP4Cover(response.content, imageformat=MP4Cover.FORMAT_PNG)


def _download_cover_art(cover_url: Optional[str]) -> Optional[MP4Cover]:
    """Download the cover art image and wrap it as an MP4Cover tag.
    Returns None if there's no URL or the download fails -- a missing
    cover shouldn't stop the rest of the tags from being written.
    v0.17.3: a PNG from the server first when Cover art is "PNG";
    v0.17.4: failing that, the usual cover converted to PNG."""
    if not cover_url:
        return None

    want_png = download_settings.get_setting()["cover_format"] == "png"
    if want_png:
        cover = _download_png_cover(cover_url)
        if cover:
            return cover

    try:
        response = requests.get(cover_url, timeout=10)
        response.raise_for_status()
    except Exception as e:
        logger.log_error(f"Failed to download cover art from {cover_url}: {e}")
        return None

    if want_png and response.content[:8] != _PNG_SIGNATURE:
        png = _convert_cover_to_png(response.content, cover_url)
        if png:
            return MP4Cover(png, imageformat=MP4Cover.FORMAT_PNG)

    image_format = MP4Cover.FORMAT_JPEG
    content_type = response.headers.get("Content-Type", "")
    if cover_url.lower().endswith(".png") or "png" in content_type:
        image_format = MP4Cover.FORMAT_PNG

    return MP4Cover(response.content, imageformat=image_format)


COVER_CONVERT_TIMEOUT_SECONDS = 30


def _convert_cover_to_png(data: bytes, cover_url: str) -> Optional[bytes]:
    """The image (JPEG, WebP...) re-saved as a PNG with ffmpeg (v0.17.4),
    or None if ffmpeg is missing or fails -- the caller then embeds the
    original. PNG is lossless, so the picture is exactly the decoded
    original; accurate rounding and full chroma interpolation keep the
    colour conversion as close as ffmpeg gets."""
    ffmpeg = shutil.which("ffmpeg")  # as downloader.ffmpeg_path()
    if not ffmpeg:
        logger.log_event(f"ffmpeg wasn't found, so the cover from {cover_url} is kept as it is (not PNG)")
        return None
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-i", "pipe:0",
           "-frames:v", "1", "-sws_flags", "accurate_rnd+full_chroma_int", "-c:v", "png",
           "-f", "image2pipe", "pipe:1"]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0  # no console flash from the GUI
    try:
        done = subprocess.run(cmd, input=data, capture_output=True, timeout=COVER_CONVERT_TIMEOUT_SECONDS,
                              creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.log_error(f"Couldn't convert the cover from {cover_url} to PNG: {e}")
        return None
    if done.returncode != 0 or done.stdout[:8] != _PNG_SIGNATURE:
        err = done.stderr.decode("utf-8", "replace").strip()
        logger.log_error(f"ffmpeg couldn't convert the cover from {cover_url} to PNG: {err or 'no PNG made'}")
        return None
    return done.stdout


def _safe_unlink(path: Path) -> None:
    """Best-effort delete used only for cleaning up our own temp files
    after a failed staged operation -- never raises."""
    try:
        path.unlink()
    except OSError:
        pass


def tag_file(
    file_path: Path,
    track,
    identity_id: str,
    youtube_id: Optional[str] = None,
    total_tracks: Optional[int] = None,
    lyrics: Optional[str] = None,
    fallback_cover: Optional[MP4Cover] = None,
) -> bool:
    """
    Burn the project's metadata field set into file_path.

    track: either a spotify_reader.Track or a youtube_playlist_reader.
    YouTubeTrack -- anything exposing .name, .primary_artist,
    .album_name, .year, .list_position, and .cover_url. This function
    doesn't care which platform it came from; it only reads that
    shared shape.

    identity_id: the exact string to burn into the Comment field --
    the diff/identity key the Library Sync/Diff Engine reads back
    later (design doc §3.3). For a Spotify track this is the bare
    Spotify Track ID (unchanged since v0.1.0); for a YouTube-native
    track it's build_youtube_identity(video_id) (Checklist item 6,
    v0.15.0, "yt:<videoId>"). Passed explicitly by the caller rather
    than derived from `track` here, so this function stays agnostic to
    which kind of Track it was given.

    youtube_id: the YouTube video ID actually downloaded -- written to
    Grouping/Composer for tracing via Exceptions (design doc §3.1).
    For a YouTube-native track this is normally the same video as
    identity_id encodes, except when an exception override substituted
    a different video -- see library_sync.fix_track().

    total_tracks is the playlist's total track count (for the "3 of 87"
    style pairing in the Track # field) -- pass it if known, it's fine
    to omit.

    fallback_cover (v0.16.40): cover art written only when the track's
    own cover_url gives none -- an imported file's embedded picture.
    youtube_id may also be an "Own file: ..." marker for an imported song.

    Returns True on success, False if tagging failed (check
    logs/error_log.txt for why). The audio file itself is left alone
    either way -- a failed tag attempt doesn't touch the audio data.

    v0.16.45: an .mp3 / .wav / .flac / .ogg file gets the same fields in
    its own tags (module docstring); an .m4a is tagged exactly as before.
    """
    try:
        family = _family(file_path)
    except ValueError as e:
        logger.log_error(f"Failed to open {file_path} for tagging: {e}")
        return False
    if family != "mp4":
        return _tag_other(file_path, family, track, identity_id, youtube_id, total_tracks, lyrics,
                          fallback_cover)
    try:
        audio = MP4(file_path)
    except Exception as e:
        logger.log_error(f"Failed to open {file_path} for tagging: {e}")
        return False

    audio["\xa9nam"] = [track.name]
    audio["\xa9ART"] = [track.primary_artist]

    if track.album_name:
        audio["\xa9alb"] = [track.album_name]

    if track.year:
        audio["\xa9day"] = [str(track.year)]

    audio["trkn"] = [(track.list_position, total_tracks or 0)]

    disc, discs = _disc_of(track)
    if disc:
        audio["disk"] = [(disc, discs)]

    cover = _download_cover_art(track.cover_url) or fallback_cover
    if cover:
        audio["covr"] = [cover]

    if lyrics:
        audio["\xa9lyr"] = [lyrics]

    # Comment field: this track's stable diff/identity key -- see
    # design doc Section 3.3 and the module docstring above.
    audio["\xa9cmt"] = [identity_id]

    # Grouping field: YouTube video ID actually downloaded -- see
    # design doc Section 3.1. Kept for backward compatibility with
    # every file tagged before v0.13.0.
    if youtube_id:
        audio[LEGACY_YOUTUBE_ID_ATOM] = [youtube_id]

        # Composer field: duplicate of the same YouTube video ID,
        # written specifically because Windows 11's Explorer Properties
        # panel doesn't surface the Grouping atom at all (Checklist
        # item 5). This intentionally repurposes Composer for tracing
        # rather than an actual composer credit -- see module docstring.
        audio[WINDOWS_VISIBLE_YOUTUBE_ID_ATOM] = [youtube_id]

    try:
        audio.save()
    except Exception as e:
        logger.log_error(f"Failed to save tags to {file_path}: {e}")
        return False

    return True


def _disc_of(track) -> tuple:
    """(disc number, how many discs) of an album track; (0, 0) for a
    playlist track, which gets no Disc # (v0.16.48)."""
    try:
        disc = int(getattr(track, "disc_number", 0) or 0)
        discs = int(getattr(track, "disc_total", 0) or 0)
    except (TypeError, ValueError):
        return 0, 0
    return (disc, max(discs, disc)) if disc > 0 else (0, 0)


def _tag_other(file_path: Path, family: str, track, identity_id: str, youtube_id: Optional[str],
               total_tracks: Optional[int], lyrics: Optional[str], fallback_cover) -> bool:
    """tag_file() for an .mp3 / .wav (ID3) or .flac / .ogg (Vorbis
    comment) file (v0.16.45): the same fields, in that format's tags."""
    try:
        audio = _open(file_path)
    except Exception as e:
        logger.log_error(f"Failed to open {file_path} for tagging: {e}")
        return False

    cover = _download_cover_art(track.cover_url) or fallback_cover
    number = track.list_position
    disc, discs = _disc_of(track)
    try:
        if family == "id3":
            tags = audio.tags
            tags.setall("TIT2", [TIT2(encoding=3, text=[track.name])])
            tags.setall("TPE1", [TPE1(encoding=3, text=[track.primary_artist])])
            if track.album_name:
                tags.setall("TALB", [TALB(encoding=3, text=[track.album_name])])
            if track.year:
                tags.setall("TDRC", [TDRC(encoding=3, text=[str(track.year)])])
            tags.setall("TRCK", [TRCK(encoding=3, text=[f"{number}/{total_tracks}" if total_tracks else str(number)])])
            if disc:
                tags.setall("TPOS", [TPOS(encoding=3, text=[f"{disc}/{discs}"])])
            if cover:
                tags.setall("APIC", [APIC(encoding=3, mime=_cover_mime(cover), type=3, desc="Cover",
                                          data=bytes(cover))])
            if lyrics:
                tags.setall("USLT", [USLT(encoding=3, lang="eng", desc="", text=lyrics)])
            tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=[identity_id])])
            if youtube_id:
                tags.setall("TIT1", [TIT1(encoding=3, text=[youtube_id])])  # Grouping
                tags.setall("TCOM", [TCOM(encoding=3, text=[youtube_id])])  # Composer (Explorer-visible)
        else:
            audio["title"] = [track.name]
            audio["artist"] = [track.primary_artist]
            if track.album_name:
                audio["album"] = [track.album_name]
            if track.year:
                audio["date"] = [str(track.year)]
            audio["tracknumber"] = [str(number)]
            if total_tracks:
                audio["tracktotal"] = [str(total_tracks)]
            if disc:
                audio["discnumber"] = [str(disc)]
                audio["disctotal"] = [str(discs)]
            if cover:
                picture = Picture()
                picture.type = 3
                picture.mime = _cover_mime(cover)
                picture.desc = "Cover"
                picture.data = bytes(cover)
                if isinstance(audio, FLAC):
                    audio.clear_pictures()
                    audio.add_picture(picture)
                else:
                    audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
            if lyrics:
                audio["lyrics"] = [lyrics]
            audio["comment"] = [identity_id]
            if youtube_id:
                audio["grouping"] = [youtube_id]
                audio["composer"] = [youtube_id]
        _save(audio, family)
    except Exception as e:
        logger.log_error(f"Failed to save tags to {file_path}: {e}")
        return False
    return True


@dataclass
class FileTags:
    """What the app reads back from a song file (v0.16.45), any format."""
    title: str = ""
    artist: str = ""
    album: str = ""
    track_number: Optional[int] = None
    disc_number: Optional[int] = None  # v0.16.48: album tracks only
    identity: Optional[str] = None  # the Comment identity (see read_track_identity())
    youtube_id: Optional[str] = None  # Composer, else Grouping (see read_youtube_id())
    length: float = 0.0
    cover: Optional[MP4Cover] = None  # only with with_cover=True


def read_tags(file_path: Path, with_cover: bool = False) -> Optional[FileTags]:
    """Read a song file's tags, whatever its format, or None if it can't
    be opened at all (logged). Used by the diff, Check Integrity and the
    Edit page, so every format is read the same way."""
    try:
        family = _family(file_path)
        audio = MP4(file_path) if family == "mp4" else _open(file_path)
    except Exception as e:
        logger.log_error(f"Failed to open {file_path} to read its tags: {e}")
        return None

    out = FileTags(length=float(getattr(audio.info, "length", 0.0) or 0.0))
    if family == "mp4":
        def first(atom):
            v = audio.get(atom)
            return str(v[0]) if v else None
        trkn = audio.get("trkn")
        out.track_number = trkn[0][0] if trkn and trkn[0] else None
        disk = audio.get("disk")
        out.disc_number = disk[0][0] if disk and disk[0] and disk[0][0] else None
        out.title, out.artist, out.album = first("\xa9nam") or "", first("\xa9ART") or "", first("\xa9alb") or ""
        out.identity = first("\xa9cmt")  # unchanged rule: any Comment is the identity
        out.youtube_id = first(WINDOWS_VISIBLE_YOUTUBE_ID_ATOM) or first(LEGACY_YOUTUBE_ID_ATOM)
    elif family == "id3":
        tags = audio.tags
        out.title = _first_text(tags.getall("TIT2")) or ""
        out.artist = _first_text(tags.getall("TPE1")) or ""
        out.album = _first_text(tags.getall("TALB")) or ""
        out.track_number = _parse_track_number(_first_text(tags.getall("TRCK")))
        out.disc_number = _parse_track_number(_first_text(tags.getall("TPOS")))
        out.identity = _id3_comment(tags)
        out.youtube_id = _first_text(tags.getall("TCOM")) or _first_text(tags.getall("TIT1"))
    else:
        def first(key):
            return _first_text(audio.get(key))
        out.title, out.artist, out.album = first("title") or "", first("artist") or "", first("album") or ""
        out.track_number = _parse_track_number(first("tracknumber"))
        out.disc_number = _parse_track_number(first("discnumber"))
        out.identity = first("comment")
        out.youtube_id = first("composer") or first("grouping")
    if family != "mp4" and not _looks_like_identity(out.identity):
        out.identity = None  # someone else's comment, not a song of this app
    if with_cover:
        try:
            out.cover = _cover_of(audio)
        except Exception as e:
            logger.log_event(f"Couldn't read the cover of {file_path}: {e}")
    return out


def read_track_identity(file_path: Path) -> Optional[str]:
    """
    Read back a file's stable diff/identity key from its Comment field
    -- this is what the Library Sync/Diff Engine uses to check what's
    already downloaded, per design doc Section 3.3 -- files are
    self-describing, no separate tracking database. Platform-neutral:
    returns whatever raw string is in Comment, whether that's a bare
    Spotify Track ID or a "yt:<videoId>" YouTube identity (Checklist
    item 6, v0.15.0) -- the diff engine treats it as an opaque string
    either way.

    Returns None if the file can't be read or has no Comment tag.

    v0.16.45: any song format (read_tags()); for a non-m4a file only a
    Comment shaped like this app's identity counts.
    """
    if Path(file_path).suffix.lower() != ".m4a":
        tags = read_tags(file_path)
        return tags.identity if tags else None
    try:
        audio = MP4(file_path)
    except Exception as e:
        logger.log_error(f"Failed to open {file_path} to read its identity tag: {e}")
        return None

    comment = audio.get("\xa9cmt")
    if not comment:
        return None
    return str(comment[0])


def read_spotify_id(file_path: Path) -> Optional[str]:
    """
    Backward-compatible alias for read_track_identity() -- kept under
    its original name for anything written against it before v0.15.0.
    For a Spotify-sourced file this returns exactly what it always
    did (the bare Spotify Track ID); for a YouTube-native file it now
    returns the "yt:<videoId>" identity instead, which is why
    library_sync.py itself calls read_track_identity() directly going
    forward rather than this alias.
    """
    return read_track_identity(file_path)


def read_youtube_id(file_path: Path) -> Optional[str]:
    """
    Read back the YouTube video ID for a file (design doc Section 3.1).

    As of Checklist item 5 (v0.13.0), the ID is written to two places:
    the Composer field (Windows 11 Explorer-visible, the source of
    truth going forward) and the Grouping field (the original location,
    kept for backward compatibility). This checks Composer first and
    falls back to Grouping, so files tagged before this change --
    which only have Grouping set -- are still read correctly without
    needing to be re-tagged.

    Returns None if the file can't be read or neither field is set.
    v0.16.45: any song format (read_tags()).
    """
    if Path(file_path).suffix.lower() != ".m4a":
        tags = read_tags(file_path)
        return tags.youtube_id if tags else None
    try:
        audio = MP4(file_path)
    except Exception as e:
        logger.log_error(f"Failed to open {file_path} to read its YouTube ID: {e}")
        return None

    composer = audio.get(WINDOWS_VISIBLE_YOUTUBE_ID_ATOM)
    if composer:
        return str(composer[0])

    grouping = audio.get(LEGACY_YOUTUBE_ID_ATOM)
    if grouping:
        return str(grouping[0])

    return None


def _cover_from_bytes(data: bytes, mime: str = "") -> Optional[MP4Cover]:
    if not data:
        return None
    is_png = "png" in (mime or "").lower() or data[:8] == _PNG_SIGNATURE
    return MP4Cover(data, imageformat=MP4Cover.FORMAT_PNG if is_png else MP4Cover.FORMAT_JPEG)


def read_embedded_cover(file_path: Path) -> Optional[MP4Cover]:
    """The cover picture embedded in any audio file mutagen can read
    (m4a, FLAC, MP3 / WAV / AIFF with ID3, Ogg / Opus), as an MP4Cover,
    or None (v0.16.40, Checklist item 47). Never raises."""
    try:
        audio = mutagen.File(file_path)
        if audio is None:
            return None
        return _cover_of(audio)
    except Exception as e:
        logger.log_event(f"Couldn't read the cover embedded in {file_path}: {e}")
    return None


def read_source_tags(file_path: Path) -> Dict[str, str]:
    """{"title", "artist", "album", "year"} from any audio file's own tags
    ("" where missing), to pre-fill the import form (v0.16.40). Never
    raises."""
    out = {"title": "", "artist": "", "album": "", "year": ""}
    try:
        audio = mutagen.File(file_path, easy=True)
        tags = audio.tags if audio is not None else None
        if tags:
            for field_name, key in (("title", "title"), ("artist", "artist"), ("album", "album"), ("year", "date")):
                value = tags.get(key)
                if value:
                    out[field_name] = str(value[0]).strip()
            out["year"] = out["year"][:4]
    except Exception as e:
        logger.log_event(f"Couldn't read the tags of {file_path}: {e}")
    return out


def update_track_number(file_path: Path, track_number: int, total_tracks: Optional[int] = None) -> bool:
    """
    Update just the Track # field on an already-tagged file, without
    re-fetching or re-writing anything else (cover art, lyrics, etc).
    Used by the Library Sync/Diff Engine when a track's position in the
    live playlist has changed but the file itself is otherwise
    unchanged and doesn't need re-downloading. Works identically for a
    Spotify-sourced or YouTube-native file -- this only ever touches
    the Track # atom.

    Checklist item 3 (crash / interruption failsafes): unlike tag_file()
    (which only ever touches a disposable, not-yet-committed staged
    download -- see acquisition_engine.py), this function edits a file
    that's already live in a synced playlist folder. To avoid any risk
    of a process being killed mid-`audio.save()` and corrupting an
    already-good file, the edit is made on a temporary copy first; the
    original is only ever replaced, atomically, once that copy has
    fully saved. If interrupted, the worst case is an orphaned
    `*<RETAG_TEMP_SUFFIX>` file next to an untouched original -- which
    library_sync.check_playlist_integrity() will flag for cleanup.

    Returns True on success, False if it failed (check logs/error_log.txt).

    v0.16.45: any song format; the temp copy is opened as its original's
    format, and only that format's Track # field is changed.
    """
    file_path = Path(file_path)
    tmp_path = file_path.with_suffix(file_path.suffix + RETAG_TEMP_SUFFIX)

    try:
        shutil.copy2(file_path, tmp_path)
    except OSError as e:
        logger.log_error(f"Failed to stage a temp copy of {file_path} for renumbering: {e}")
        return False

    try:
        family = _family(file_path)
        if family == "mp4":
            audio = MP4(tmp_path)
            audio["trkn"] = [(track_number, total_tracks or 0)]
            audio.save()
        elif family == "id3":
            audio = _open(tmp_path, like=file_path)
            text = f"{track_number}/{total_tracks}" if total_tracks else str(track_number)
            audio.tags.setall("TRCK", [TRCK(encoding=3, text=[text])])
            _save(audio, family)
        else:
            audio = _open(tmp_path, like=file_path)
            audio["tracknumber"] = [str(track_number)]
            if total_tracks:
                audio["tracktotal"] = [str(total_tracks)]
            _save(audio, family)
    except Exception as e:
        logger.log_error(f"Failed to update track number (staged copy) for {file_path}: {e}")
        _safe_unlink(tmp_path)
        return False

    try:
        os.replace(tmp_path, file_path)
    except OSError as e:
        logger.log_error(f"Failed to commit updated track number for {file_path}: {e}")
        _safe_unlink(tmp_path)
        return False

    return True
