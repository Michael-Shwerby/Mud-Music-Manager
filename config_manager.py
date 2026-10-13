"""
Mud Music Manager - Config Manager

Handles playlists.json: the list of tracked playlists (add/remove/
rename) and the library folder path. Each tracked-playlist entry
records which platform it lives on natively (`platform`: "spotify" or
"youtube", Checklist item 6) plus that platform's own playlist ID
(`spotify_id` or `youtube_playlist_id`) and a display name
(folder_name) -- no per-song data lives here, since that's carried in
the downloaded files themselves. An entry may also carry an optional
custom_path, pointing its files at any folder on disk instead of the
default location under the Library path.

Checklist item 3 (crash / interruption failsafes):
  - playlists.json writes are now atomic (see _atomic_write_json()) --
    a process killed mid-write can no longer leave a corrupted or
    truncated config file behind.
  - rename_playlist()'s folder-rename-on-disk step is now recoverable
    if the process is killed between the disk rename and the config
    save that should follow it -- see _recover_pending_folder_rename(),
    run automatically by load_config().

Checklist item 4 (Music Folder path change -- move-or-relocate UX):
  - change_library_path() is the new entry point for changing the
    Library folder, replacing a bare repoint-only operation with a
    choice: repoint only (old behavior, still the default), or also
    physically move every currently-tracked playlist's folder (except
    ones with a custom_path, which live outside the Library folder on
    purpose and are never touched here) into the new location.
  - The move itself is built on the same crash-safety pattern as the
    rest of Checklist item 3: each playlist folder is fully copied to
    the new location under a temp name first, swapped into place with
    an atomic os.replace() once the copy is verified complete, and
    only then is the old copy deleted -- see _do_move_playlist_folder().
    A `_pending_library_move` breadcrumb is saved to config *before*
    any folder is touched, so a crash partway through is fully
    recoverable on the next app launch (see
    _recover_pending_library_move(), run automatically by
    load_config()) -- exactly the same "record intent before touching
    disk, self-heal on next load" shape as rename_playlist()'s existing
    folder-rename recovery, just generalized to a whole batch of
    folders. config["library_path"] itself is only flipped over to the
    new location once every folder is confirmed moved, so the app
    always has a fully-valid library_path to fall back on if
    interrupted.
  - main.py explicitly announces which of these two things it's doing
    ("Repointing..." / "Moving all files...") right after the user
    picks one, offers a `cancel`/`done`/`back` out at that same prompt,
    and -- once the change is complete -- runs a real Scan (Checklist
    item 2's `library_sync.scan_playlist()`, not just a filesystem
    presence check) against every affected tracked playlist, so it's
    immediately clear whether each one is still fully in sync at the
    new location, not just whether a folder happens to exist there.

Checklist item 6 (YouTube Playlist sync/download, v0.15.0):
  - Every playlist entry carries a `platform` field
    (PLATFORM_SPOTIFY / PLATFORM_YOUTUBE). add_playlist() detects which
    platform a pasted link/ID belongs to automatically (Spotify tried
    first, then YouTube -- design Q6), rather than requiring a separate
    menu path: "Add -> Download & Sync It" now accepts either kind of
    link transparently.
  - Schema (design Q5, option C -- keep separate ID keys, add an
    accessor): a Spotify entry still has `spotify_id`, unchanged since
    v0.1.0. A YouTube-native entry instead has `youtube_playlist_id`.
    Nothing reads either key directly anymore outside this module --
    get_playlist_source_id(entry) resolves the right one based on
    `platform`, so callers never need to check platform themselves just
    to find a playlist's ID. This was chosen over renaming `spotify_id`
    to a generic key (which would have required a one-time schema
    migration and touched every existing read of that key) or adding a
    parallel key with no accessor (which would leave every caller open
    to crashing on the wrong key for a YouTube entry).
  - update_playlist_spotify_id() is superseded by
    update_playlist_source_id(), which works for either platform
    (kept as a backward-compatible alias under the old name).

Checklist item 16 (playlist URL editable on the GUI's Edit page, v0.16.12):
  - update_playlist_source_id() gains an opt-in allow_platform_change
    flag (default False, so the terminal menu's Edit -> URL behaves
    exactly as before). With it on, the new link's platform is detected
    the same way add_playlist() does (detect_playlist_source(), new),
    and a Spotify <-> YouTube switch updates the entry's `platform` and
    swaps its ID key (`spotify_id` <-> `youtube_playlist_id`), so the
    entry always holds exactly one ID key, matching its platform. A link
    another tracked playlist already uses is refused. Only the GUI's
    Edit page passes True (approved by Michael, 2026-10-07).

Checklist item 41a (Spotify albums, v0.16.48):
  - A Spotify album link can be tracked like a playlist. The entry keeps
    `platform` "spotify" and adds `kind` "album" (KIND_ALBUM; a missing
    `kind` means KIND_PLAYLIST, so every existing entry is unchanged), with
    the album's ID under its own key, `spotify_album_id`.
    get_playlist_source_id() returns it, so exceptions, track failures and
    the GUI cache key on the album ID like any playlist ID.
  - detect_source() returns (platform, kind, id) for a pasted link,
    album first (only a link or spotify:album: URI counts as an album; a
    bare 22-character ID is still a playlist), then playlist, then YouTube.
    entry_source() gives the same triple for an entry, and add_playlist() /
    update_playlist_source_id() compare those triples. A Spotify artist
    link is recognised only to say it isn't supported yet (item 41b).

Sections (v0.16.48 follow-up, v0.16.49): the list is kept in two
sections, every playlist first and then every album (group_playlists(),
run by load_config(), add_playlist() and a kind-changing link change),
so the Main Menu and the terminal menu can show albums as their own list
below the playlists while positions stay one running count. Each section
keeps its own order, and reorder_playlist() only moves an entry within
its section (section_range()).

Artists (v0.16.50, Checklist items 41b / 41c): config["artists"] holds
the artists added from a discography link (see artist_watch.py, which
owns that list). An album added for one carries "artist_id"; each tracked
artist's albums are a section of their own, after the playlists and
before the albums added on their own (section_key()).

YT Music (v0.16.51, Checklist item 42a): a third platform,
PLATFORM_YTMUSIC ("ytmusic", Michael's choice: its own platform, not a
kind of YouTube playlist). Only links on music.youtube.com count (a
www.youtube.com link stays a YouTube playlist, as before):
  - a playlist: music.youtube.com/playlist?list=<id> (or browse/VL<id>)
    -> `ytmusic_playlist_id`;
  - an album: music.youtube.com/browse/MPREb_... or a playlist link whose
    list is an album's own (OLAK5uy_...) -> kind "album",
    `ytmusic_album_id` (stored as given; the reader accepts both);
  - an artist: music.youtube.com/channel/UC... (the discography picker,
    as a Spotify artist link).
Its songs are YouTube videos, so they use the YouTube identity
("yt:<videoId>") and the YouTube sync path (uses_video_ids()). An artist
entry is {"platform": "ytmusic", "ytmusic_artist_id", ...}; artist IDs of
the two platforms can't collide (a YT Music one is "UC" + 22 characters),
so an album's "artist_id" stays a plain ID and artist_id_of() reads
either key.

SoundCloud (v0.16.52, Checklist item 42b): a fourth platform,
PLATFORM_SOUNDCLOUD. A set link (soundcloud.com/<user>/sets/<name>, with
its "/s-..." secret part for a private one) is tracked by that path,
`soundcloud_set` ("<user>/sets/<name>[/s-...]"), as a playlist or an album
(kind "album"). A set link looks the same either way: only SoundCloud's
own data says which, so whoever adds one looks it up first and passes
add_playlist(..., kind=). Comparisons of SoundCloud sources ignore the
kind (same_source()). A short link (on.soundcloud.com/...) must be looked
up (followed) before it can be recognised. Songs are identified by
"sc:<track id>" (metadata_tagger).

Apple Music (v0.16.53, Checklist item 42c): a fifth platform,
PLATFORM_APPLE_MUSIC. Its songs can't be downloaded, so it works like
Spotify (Michael's choice): the playlist is read, each song is searched
for on YouTube. Links are music.apple.com/<country>/playlist|album|artist/
[<name>/]<id>; the IDs stored keep the country, since Apple's catalogue is
per country: `applemusic_playlist` "us/pl.xxxx", `applemusic_album`
"us/697194953" (kind "album"), an artist's "us/artist/5468295" (in
"artists", `applemusic_artist_id`). Those shapes can't be confused with
another platform's IDs (id_platform()). Songs are identified by
"am:<Apple song ID>".

Amazon Music (v0.16.54, Checklist item 42d): a sixth platform,
PLATFORM_AMAZON_MUSIC, also working like Spotify. Links are
music.amazon.<site>[/<locale>]/playlists|user-playlists|albums/<id>; the
ID stored keeps the site, "music.amazon.com/B0868WH5XX", under
`amazonmusic_playlist` or (kind "album") `amazonmusic_album`. An artist
link is refused (Amazon publishes no artist discography). Songs are
identified by "amz:<song ASIN>".
"""

import json
import os
import re
import shutil
from pathlib import Path

import app_paths
import logger

CONFIG_FILENAME = "playlists.json"
# v0.18.0: in the user's data folder (app_paths.DATA_DIR), not the app folder.
CONFIG_PATH = app_paths.data_path(CONFIG_FILENAME)

# Suffix used for the temporary copy a Library-folder move
# (change_library_path()) stages a playlist folder's contents into
# before atomically swapping it into its final new location. Kept as a
# module-level constant, matching the same convention as
# metadata_tagger.RETAG_TEMP_SUFFIX, in case a future integrity-style
# scan ever wants to recognize leftovers from an interrupted move.
LIBRARY_MOVE_TMP_SUFFIX = ".mmm_move_tmp"

# Which service a tracked playlist lives on natively (Checklist item 6).
PLATFORM_SPOTIFY = "spotify"
PLATFORM_YOUTUBE = "youtube"
PLATFORM_YTMUSIC = "ytmusic"  # v0.16.51 (Checklist item 42a)
PLATFORM_SOUNDCLOUD = "soundcloud"  # v0.16.52 (Checklist item 42b)
PLATFORM_APPLE_MUSIC = "applemusic"  # v0.16.53 (Checklist item 42c)
PLATFORM_AMAZON_MUSIC = "amazonmusic"  # v0.16.54 (Checklist item 42d)

# Platforms whose songs are searched for on YouTube (the tiered search,
# §3.4), identified by their own catalogue ID: Spotify and, since v0.16.53,
# Apple Music (v0.16.54: and Amazon Music).
SEARCH_PLATFORMS = (PLATFORM_SPOTIFY, PLATFORM_APPLE_MUSIC, PLATFORM_AMAZON_MUSIC)

_AMAZON_LINK = re.compile(r"(?:https?://)?(music\.amazon\.[a-z.]+?)/(?:[a-z]{2}-[a-z]{2}/)?"
                          r"(playlists|user-playlists|albums|artists)/([A-Za-z0-9]+)", re.IGNORECASE)
_AMAZON_ID = re.compile(r"music\.amazon\.[a-z.]+/[A-Za-z0-9]+")

_APPLE_LINK = re.compile(r"(?:https?://)?(?:geo\.)?music\.apple\.com/([a-z]{2})/(playlist|album|artist)/"
                         r"(?:[^/?#\s]+/)?([^/?#\s]+)", re.IGNORECASE)
_APPLE_ID = re.compile(r"[a-z]{2}/(?:pl\.[A-Za-z0-9.-]+|\d+|artist/\d+)")

# Platforms whose songs are YouTube videos, identified by "yt:<videoId>"
# and synced by downloading the listed video (no search): YouTube and,
# since v0.16.51, YT Music.
VIDEO_PLATFORMS = (PLATFORM_YOUTUBE, PLATFORM_YTMUSIC)

# A YT Music album's browse ID / its own playlist's ID, and an artist's
# channel ID (v0.16.51).
_YTM_ALBUM_BROWSE = re.compile(r"MPREb_[A-Za-z0-9_-]+")
_YTM_ALBUM_PLAYLIST = re.compile(r"OLAK5uy_[A-Za-z0-9_-]+")
_YTM_ARTIST = re.compile(r"UC[A-Za-z0-9_-]{22}")

# What kind of thing an entry tracks (Checklist item 41a, v0.16.48). An
# entry without `kind` is a playlist.
KIND_PLAYLIST = "playlist"
KIND_ALBUM = "album"

ARTIST_NOT_SUPPORTED = (
    "That's an artist link. An artist is added with + Add, which lets you choose "
    "its albums and singles; it can't be a playlist's own link."
)

DEFAULT_CONFIG = {
    "library_path": None,
    "playlists": [],
    # v0.16.50 (Checklist items 41b / 41c): artists added from a discography,
    # see artist_watch.py. Their albums are ordinary album entries in
    # "playlists" with an "artist_id".
    "artists": [],
}


def _atomic_write_json(path, data):
    """
    Write JSON to `path` without ever leaving a half-written or
    corrupted file behind if the process is killed mid-write
    (Checklist item 3 -- this used to be a direct overwrite).

    Writes the full content to a temp file in the same directory
    first, flushes and fsyncs it, then swaps it into place with
    os.replace() -- a single atomic filesystem operation on both
    Windows and POSIX, so `path` always holds either the complete old
    contents or the complete new contents, never a partial write. The
    previous good version is also kept alongside as a `.bak` file,
    best-effort, as a manual recovery option if something else goes
    wrong later.
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
            # Non-fatal -- the backup is a bonus safety net, not the
            # primary safeguard (that's the atomic swap below).
            logger.log_error(f"Couldn't refresh backup copy of {path.name}: {e}")

    os.replace(tmp_path, path)


def _recover_pending_folder_rename(config):
    """
    Self-heals a playlist folder rename that was interrupted between
    the disk move and the config save that should have followed it
    (Checklist item 3 -- see rename_playlist()). If a
    `_pending_folder_rename` marker is found left over from a crash,
    checks which directory actually exists on disk and finishes
    whichever half of the operation didn't complete, rather than
    silently leaving the config pointing at a folder name that no
    longer matches reality (which would otherwise make the real,
    renamed folder invisible to the app, and risk a duplicate
    re-download under the old name on the next sync).
    """
    pending = config.pop("_pending_folder_rename", None)
    if not pending:
        return

    old_dir, new_dir = pending["old_dir"], pending["new_dir"]
    old_name, new_name = pending["old_folder_name"], pending["new_folder_name"]

    if os.path.isdir(new_dir):
        # The disk rename completed but the config save never did --
        # finish updating the entry now.
        entry = find_playlist(config, old_name)
        if entry:
            entry["folder_name"] = new_name
            logger.log_event(
                f"Recovered an interrupted rename: folder was already moved to "
                f"'{new_name}' on disk -- updated the config entry to match."
            )
    elif os.path.isdir(old_dir):
        # The disk rename itself never happened -- the entry's old
        # folder_name is still correct as-is, nothing to fix.
        logger.log_event(
            f"Recovered an interrupted rename: the folder was never actually "
            f"moved, leaving '{old_name}' as-is."
        )
    # else: neither directory exists (e.g. manually deleted in the
    # meantime) -- nothing sensible to recover, so the entry is left
    # alone rather than guessed at.

    save_config(config)


def _do_move_playlist_folder(old_dir, new_dir):
    """
    Crash-safe move of a single playlist folder from old_dir to new_dir
    (Checklist item 4). Never leaves a half-copied folder sitting at
    new_dir: the folder's full contents are copied to a temp name next
    to new_dir first, then swapped into place with a single atomic
    os.replace() only once that copy has completely finished, and only
    THEN is the original at old_dir removed.

    Idempotent by design, so it's safe to call again when resuming an
    interrupted move (see _recover_pending_library_move()):
      - If old_dir doesn't exist, there's nothing to move -- reports
        success (nothing left to do).
      - If new_dir already exists, the move already completed on a
        previous attempt -- reports success without touching anything.
      - If a leftover temp copy from an interrupted previous attempt
        is found, it's discarded (an unfinished copy can't be trusted)
        and the copy is redone from old_dir, which is always left
        completely untouched until the very last step.

    Returns True on success, False if it failed partway through (in
    which case old_dir is left exactly as it was -- check
    logs/error_log.txt for the specific reason).
    """
    old_dir, new_dir = Path(old_dir), Path(new_dir)
    tmp_dir = new_dir.with_name(new_dir.name + LIBRARY_MOVE_TMP_SUFFIX)

    if not old_dir.exists():
        return True  # nothing to move -- already "done" in effect

    if new_dir.exists():
        return True  # already moved on a previous attempt

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        shutil.copytree(old_dir, tmp_dir)
    except OSError as e:
        logger.log_error(f"Failed to copy '{old_dir}' to '{tmp_dir}' during library move: {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False

    try:
        os.replace(tmp_dir, new_dir)
    except OSError as e:
        logger.log_error(
            f"Copied '{old_dir}' successfully but failed to move it into place at "
            f"'{new_dir}': {e}. The copy is left at '{tmp_dir}' rather than discarded."
        )
        return False

    try:
        shutil.rmtree(old_dir)
    except OSError as e:
        # Not fatal -- the folder is safely and fully present at its
        # new location either way; this just leaves an unwanted
        # duplicate at the old path that Michael would need to clean
        # up manually.
        logger.log_error(
            f"Moved '{old_dir}' to '{new_dir}' successfully, but couldn't remove the "
            f"now-redundant old copy at '{old_dir}': {e}"
        )

    return True


def _recover_pending_library_move(config):
    """
    Self-heals a Library-folder move (Checklist item 4 -- see
    change_library_path()) that was interrupted partway through.
    Resumes moving whichever playlist folders hadn't finished yet,
    using the exact same per-folder logic as a normal move, and only
    flips config["library_path"] over to the new location once every
    folder involved is confirmed done -- so an interrupted move can
    never leave the app pointed at a location that isn't fully ready.
    """
    pending = config.get("_pending_library_move")
    if not pending:
        return

    old_library_path = pending["old_library_path"]
    new_library_path = pending["new_library_path"]
    statuses = pending.get("folders", {})

    all_done = True
    for folder_name, status in list(statuses.items()):
        if status == "done":
            continue
        old_dir = os.path.join(old_library_path, folder_name)
        new_dir = os.path.join(new_library_path, folder_name)
        if _do_move_playlist_folder(old_dir, new_dir):
            statuses[folder_name] = "done"
            logger.log_event(
                f"Recovered an interrupted library move: finished moving '{folder_name}' "
                f"to '{new_library_path}'."
            )
        else:
            all_done = False
            logger.log_error(
                f"Could not finish recovering the interrupted library move for "
                f"'{folder_name}' -- will retry again on next launch."
            )

    pending["folders"] = statuses

    if all_done:
        config["library_path"] = new_library_path
        config.pop("_pending_library_move", None)
        logger.log_event(
            f"Recovered an interrupted library move: all playlist folders confirmed at "
            f"'{new_library_path}' -- library path updated to match."
        )

    save_config(config)


def _backfill_playlist_platforms(config):
    """
    Ensures every playlist entry has a `platform` key (Checklist item
    6). Any entry from a config file written before v0.14.0 defaults to
    PLATFORM_SPOTIFY -- every playlist tracked before that version was,
    and still is, Spotify-sourced.
    """
    for entry in config.get("playlists", []):
        entry.setdefault("platform", PLATFORM_SPOTIFY)


def load_config():
    """Load the config file, creating a default one if it doesn't exist yet."""
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.log_error(f"Failed to read {CONFIG_FILENAME}: {e}")
        raise SystemExit(
            f"Could not read {CONFIG_FILENAME} -- it may be corrupted. "
            f"Check logs/error_log.txt for details."
        )

    # Backfill any missing keys so older config files still load cleanly
    config.setdefault("library_path", None)
    config.setdefault("playlists", [])
    config.setdefault("artists", [])
    _backfill_playlist_platforms(config)
    group_playlists(config)

    _recover_pending_folder_rename(config)
    _recover_pending_library_move(config)

    return config


def save_config(config):
    """Write the config dict to disk. Hardened against a mid-write
    crash (Checklist item 3) -- see _atomic_write_json()."""
    try:
        _atomic_write_json(CONFIG_PATH, config)
    except OSError as e:
        logger.log_error(f"Failed to write {CONFIG_FILENAME}: {e}")
        raise


def extract_playlist_id(spotify_input):
    """
    Accept a full Spotify playlist URL or a bare playlist ID and
    return just the ID. Returns None if nothing recognizable is found.

    All of these resolve to '37i9dQZF1DXcBWIGoYBM5M':
        https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
        https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123
        spotify:playlist:37i9dQZF1DXcBWIGoYBM5M
        37i9dQZF1DXcBWIGoYBM5M
    """
    spotify_input = spotify_input.strip()

    url_match = re.search(r"playlist[/:]([a-zA-Z0-9]+)", spotify_input)
    if url_match:
        return url_match.group(1)

    # Bare ID fallback -- Spotify IDs are 22-character base62 strings
    if re.fullmatch(r"[a-zA-Z0-9]{22}", spotify_input):
        return spotify_input

    return None


def extract_youtube_playlist_id(youtube_input):
    """
    Accept a YouTube playlist URL (either a direct playlist link, or a
    video link that also carries a `list=` playlist parameter, which is
    what copying a link while playing something IN a playlist usually
    gives you) or a bare playlist ID, and return just the ID. Returns
    None if nothing recognizable is found (Checklist item 6, v0.15.0).

    All of these resolve to 'PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf':
        https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf
        https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf
        PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf

    Only tried by add_playlist() AFTER extract_playlist_id() has
    already failed to recognize the input as a Spotify link, so a bare
    Spotify ID (exactly 22 base62 characters, no "-"/"_") is never
    mistaken for a YouTube playlist ID here -- real-world YouTube
    playlist IDs are a different length and commonly contain "-"/"_",
    but the length check below is kept as an explicit extra safeguard
    against that specific ambiguity regardless.
    """
    youtube_input = youtube_input.strip()

    list_match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", youtube_input)
    if list_match:
        return list_match.group(1)

    # Bare ID fallback. YouTube playlist IDs vary in length (commonly
    # ~13-42 characters) and are never exactly the 22-character,
    # hyphen/underscore-free shape a Spotify ID always is.
    if re.fullmatch(r"[A-Za-z0-9_-]{10,48}", youtube_input) and len(youtube_input) != 22:
        return youtube_input

    return None


def detect_playlist_source(source_input):
    """
    Work out which platform a pasted playlist link/ID belongs to, the
    same way add_playlist() does (Spotify tried first, then YouTube).
    Returns (platform, source_id), or None if it's neither (Checklist
    item 16, v0.16.12 -- used by update_playlist_source_id()'s
    allow_platform_change path and by the GUI's Edit page).
    """
    spotify_id = extract_playlist_id(source_input)
    if spotify_id:
        return PLATFORM_SPOTIFY, spotify_id
    youtube_playlist_id = extract_youtube_playlist_id(source_input)
    if youtube_playlist_id:
        return PLATFORM_YOUTUBE, youtube_playlist_id
    return None


def extract_spotify_album_id(spotify_input):
    """
    The album ID of a Spotify album link or URI, or None (Checklist item
    41a, v0.16.48). Only a link or URI counts: a bare 22-character ID
    stays a playlist ID, as it always was.

        https://open.spotify.com/album/2noRn2Aes5aoNVsU6iWThc?si=abc
        https://open.spotify.com/intl-de/album/2noRn2Aes5aoNVsU6iWThc
        spotify:album:2noRn2Aes5aoNVsU6iWThc
    """
    if _is_apple_music_link(spotify_input):
        return None  # v0.16.53: an Apple Music link's name part could look like an ID
    match = re.search(r"album[/:]([a-zA-Z0-9]{22})(?![a-zA-Z0-9])", (spotify_input or "").strip())
    return match.group(1) if match else None


def extract_spotify_artist_id(spotify_input):
    """The artist ID of a Spotify artist link or URI, or None (v0.16.48)."""
    if _is_apple_music_link(spotify_input):
        return None
    match = re.search(r"artist[/:]([a-zA-Z0-9]{22})(?![a-zA-Z0-9])", (spotify_input or "").strip())
    return match.group(1) if match else None


def _is_ytmusic_link(text):
    """Whether this is a music.youtube.com link (v0.16.51). A bare ID is
    never one: bare playlist IDs stay YouTube, as before."""
    return bool(re.match(r"(https?://)?music\.youtube\.com/", (text or "").strip(), re.IGNORECASE))


def extract_ytmusic_album_id(ytm_input):
    """
    The album of a YT Music album link, or None (v0.16.51, Checklist item
    42a): its browse ID (MPREb_...) or its own playlist's ID (OLAK5uy_...),
    as given -- ytmusic_reader accepts both. A bare MPREb_ ID counts too
    (it can only be a YT Music album).

        https://music.youtube.com/browse/MPREb_7ltM34kr0mH
        https://music.youtube.com/playlist?list=OLAK5uy_mz6eafmqdRHSaR4IwG0ll6J6rgv0_ZpGw
    """
    text = (ytm_input or "").strip()
    if _YTM_ALBUM_BROWSE.fullmatch(text):
        return text
    if not _is_ytmusic_link(text):
        return None
    match = re.search(r"/browse/(MPREb_[A-Za-z0-9_-]+)", text)
    if match:
        return match.group(1)
    match = re.search(r"[?&]list=(OLAK5uy_[A-Za-z0-9_-]+)", text)
    return match.group(1) if match else None


def extract_ytmusic_playlist_id(ytm_input):
    """The playlist ID of a YT Music playlist link, or None (v0.16.51):
    music.youtube.com/playlist?list=<id> (a watch link with list= too) or
    music.youtube.com/browse/VL<id>. An album's own playlist (OLAK5uy_)
    is an album, not a playlist (extract_ytmusic_album_id())."""
    text = (ytm_input or "").strip()
    if not _is_ytmusic_link(text):
        return None
    match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", text) or re.search(r"/browse/VL([A-Za-z0-9_-]+)", text)
    if not match or match.group(1).startswith("OLAK5uy_"):
        return None
    return match.group(1)


def extract_ytmusic_artist_id(ytm_input):
    """The channel ID of a YT Music artist link (music.youtube.com/
    channel/UC... or /browse/UC...), or None (v0.16.51)."""
    text = (ytm_input or "").strip()
    if not _is_ytmusic_link(text):
        return None
    match = re.search(r"/(?:channel|browse)/(UC[A-Za-z0-9_-]{22})(?![A-Za-z0-9_-])", text)
    return match.group(1) if match else None


def extract_soundcloud_set(sc_input):
    """The path of a SoundCloud set (playlist or album) link,
    "<user>/sets/<name>" plus "/s-<secret>" for a private one, or None
    (v0.16.52, Checklist item 42b). Query strings (?si=..., utm_...) and
    the "m." / "www." hosts are ignored.

        https://soundcloud.com/chillhopdotcom/sets/chillhop-essentials-winter-2023?si=abc
        https://soundcloud.com/someone/sets/my-mix/s-AbCdEf123
    """
    text = (sc_input or "").strip()
    match = re.match(r"(?:https?://)?(?:www\.|m\.)?soundcloud\.com/([^/?#\s]+)/sets/([^/?#\s]+)"
                     r"(?:/(s-[A-Za-z0-9]+))?", text, re.IGNORECASE)
    if not match:
        return None
    user, name, secret = match.groups()
    return f"{user}/sets/{name}" + (f"/{secret}" if secret else "")


def is_soundcloud_short_link(text):
    """Whether this is a SoundCloud short link (on.soundcloud.com/...,
    what the app's Share button gives), which has to be followed to know
    what it is (v0.16.52)."""
    return bool(re.match(r"(https?://)?on\.soundcloud\.com/\S+", (text or "").strip(), re.IGNORECASE))


def _is_soundcloud_link(text):
    return bool(re.match(r"(https?://)?([a-z]+\.)?soundcloud\.com/", (text or "").strip(), re.IGNORECASE))


def soundcloud_link(set_path):
    """The web link of a SoundCloud set path (v0.16.52)."""
    return f"https://soundcloud.com/{set_path}"


def same_source(a, b):
    """Whether two (platform, kind, id) sources are the same thing
    (v0.16.52). A SoundCloud set link doesn't say whether it is an album,
    so for SoundCloud the kind is ignored."""
    if not a or not b:
        return False
    a, b = tuple(a), tuple(b)
    if a[0] == b[0] == PLATFORM_SOUNDCLOUD:
        return a[2] == b[2]
    return a == b


def _apple_link(text, what):
    match = _APPLE_LINK.match((text or "").strip())
    if not match or match.group(2).lower() != what:
        return None
    return match.group(1).lower(), match.group(3)


def extract_apple_music_playlist(am_input):
    """"us/pl.xxxx" for an Apple Music playlist link, or None (v0.16.53)."""
    found = _apple_link(am_input, "playlist")
    return f"{found[0]}/{found[1]}" if found and found[1].startswith("pl.") else None


def extract_apple_music_album(am_input):
    """"us/697194953" for an Apple Music album link (a song's link inside
    an album, "...?i=<song>", is the album too), or None (v0.16.53)."""
    found = _apple_link(am_input, "album")
    return f"{found[0]}/{found[1]}" if found and found[1].isdigit() else None


def extract_apple_music_artist(am_input):
    """"us/artist/5468295" for an Apple Music artist link, or None (v0.16.53)."""
    found = _apple_link(am_input, "artist")
    return f"{found[0]}/artist/{found[1]}" if found and found[1].isdigit() else None


def _amazon_link(text):
    match = _AMAZON_LINK.match((text or "").strip())
    return (match.group(1).lower(), match.group(2).lower(), match.group(3)) if match else None


def extract_amazon_music_playlist(amz_input):
    """"music.amazon.com/B0868WH5XX" for an Amazon Music playlist or a
    shared user playlist link, or None (v0.16.54)."""
    found = _amazon_link(amz_input)
    return f"{found[0]}/{found[2]}" if found and found[1] in ("playlists", "user-playlists") else None


def extract_amazon_music_album(amz_input):
    """"music.amazon.com/B0064UPU4G" for an Amazon Music album link, or None (v0.16.54)."""
    found = _amazon_link(amz_input)
    return f"{found[0]}/{found[2]}" if found and found[1] == "albums" else None


def _is_amazon_music_link(text):
    return bool(re.match(r"(https?://)?music\.amazon\.", (text or "").strip(), re.IGNORECASE))


def _is_apple_music_link(text):
    return bool(re.match(r"(https?://)?(geo\.)?music\.apple\.com/", (text or "").strip(), re.IGNORECASE))


def extract_album_id(source_input):
    """The album ID of a Spotify, (v0.16.51) YT Music or (v0.16.53) Apple
    Music album link, or None (v0.16.48). id_platform() says which."""
    return (extract_spotify_album_id(source_input) or extract_ytmusic_album_id(source_input)
            or extract_apple_music_album(source_input) or extract_amazon_music_album(source_input))


def extract_artist_id(source_input):
    """The artist ID of a Spotify, (v0.16.51) YT Music or (v0.16.53) Apple
    Music artist link, or None (v0.16.48 / v0.16.50). id_platform() says
    which."""
    return (extract_spotify_artist_id(source_input) or extract_ytmusic_artist_id(source_input)
            or extract_apple_music_artist(source_input))


def id_platform(source_id):
    """The platform an album or artist ID belongs to, from its shape
    (v0.16.51): a YT Music album (MPREb_ / OLAK5uy_) or artist (UC + 22
    characters) is PLATFORM_YTMUSIC; anything else is PLATFORM_SPOTIFY."""
    text = source_id or ""
    if _YTM_ALBUM_BROWSE.fullmatch(text) or _YTM_ALBUM_PLAYLIST.fullmatch(text) or _YTM_ARTIST.fullmatch(text):
        return PLATFORM_YTMUSIC
    if _APPLE_ID.fullmatch(text):  # v0.16.53: "us/697194953", "us/artist/5468295"
        return PLATFORM_APPLE_MUSIC
    if _AMAZON_ID.fullmatch(text):  # v0.16.54: "music.amazon.com/B0064UPU4G"
        return PLATFORM_AMAZON_MUSIC
    return PLATFORM_SPOTIFY


def platform_name(platform):
    """"Spotify", "YouTube", "YT Music" (v0.16.51) or "SoundCloud" (v0.16.52)."""
    return {PLATFORM_YOUTUBE: "YouTube", PLATFORM_YTMUSIC: "YT Music",
            PLATFORM_SOUNDCLOUD: "SoundCloud", PLATFORM_APPLE_MUSIC: "Apple Music",
            PLATFORM_AMAZON_MUSIC: "Amazon Music"}.get(platform, "Spotify")


def detect_source(source_input):
    """
    (platform, kind, source_id) for a pasted link / ID, or None if it's
    none of them (Checklist item 41a, v0.16.48). Tried in order: Spotify
    album, YT Music album and playlist (v0.16.51; music.youtube.com links
    only), Spotify playlist, YouTube playlist -- so a Spotify / YouTube
    playlist link or a bare ID is detected exactly as
    detect_playlist_source() does.
    """
    album_id = extract_spotify_album_id(source_input)
    if album_id:
        return PLATFORM_SPOTIFY, KIND_ALBUM, album_id
    album_id = extract_ytmusic_album_id(source_input)
    if album_id:
        return PLATFORM_YTMUSIC, KIND_ALBUM, album_id
    playlist_id = extract_ytmusic_playlist_id(source_input)
    if playlist_id:
        return PLATFORM_YTMUSIC, KIND_PLAYLIST, playlist_id
    if _is_ytmusic_link(source_input):
        return None  # a YT Music link that's no playlist or album (e.g. an artist)
    set_path = extract_soundcloud_set(source_input)  # v0.16.52 (Checklist item 42b)
    if set_path:
        # A playlist until a lookup says it's an album (add_playlist(kind=)).
        return PLATFORM_SOUNDCLOUD, KIND_PLAYLIST, set_path
    if _is_soundcloud_link(source_input):
        return None  # a single track, a profile, or a short link not looked up yet
    album_id = extract_apple_music_album(source_input)  # v0.16.53 (Checklist item 42c)
    if album_id:
        return PLATFORM_APPLE_MUSIC, KIND_ALBUM, album_id
    playlist_id = extract_apple_music_playlist(source_input)
    if playlist_id:
        return PLATFORM_APPLE_MUSIC, KIND_PLAYLIST, playlist_id
    if _is_apple_music_link(source_input):
        return None  # an artist, a song, or a private library link
    album_id = extract_amazon_music_album(source_input)  # v0.16.54 (Checklist item 42d)
    if album_id:
        return PLATFORM_AMAZON_MUSIC, KIND_ALBUM, album_id
    playlist_id = extract_amazon_music_playlist(source_input)
    if playlist_id:
        return PLATFORM_AMAZON_MUSIC, KIND_PLAYLIST, playlist_id
    if _is_amazon_music_link(source_input):
        return None  # an artist, a song, or something else
    detected = detect_playlist_source(source_input)
    if detected:
        return detected[0], KIND_PLAYLIST, detected[1]
    return None


def unrecognised_source_message(source_input):
    """Why a link couldn't be used, for add / link change (v0.16.48)."""
    if extract_artist_id(source_input):
        return ARTIST_NOT_SUPPORTED
    if is_soundcloud_short_link(source_input):
        return ("That's a SoundCloud short link. Wait a moment for it to be looked up, or paste the "
                "full soundcloud.com/…/sets/… link.")
    if _is_soundcloud_link(source_input):
        return "That SoundCloud link isn't a playlist or album (a set: soundcloud.com/…/sets/…)."
    if _is_apple_music_link(source_input):
        if "/library/" in source_input:
            return ("That's a link to your own Apple Music library, which only you can open. Share the "
                    "playlist publicly in Apple Music and paste that link (music.apple.com/…/playlist/…).")
        return "That Apple Music link isn't a playlist or album (or an artist, added with + Add)."
    if _is_amazon_music_link(source_input):
        found = _amazon_link(source_input)
        if found and found[1] == "artists":
            return ("Amazon Music artists can't be added: Amazon doesn't publish an artist's albums. Add the "
                    "albums you want one by one (their links are music.amazon.com/albums/…).")
        return "That Amazon Music link isn't a playlist or album (music.amazon.com/playlists/… or /albums/…)."
    return ("Could not recognize that as a Spotify, YT Music, Apple Music, Amazon Music or SoundCloud "
            "playlist or album, or a YouTube playlist link or ID.")


def get_library_path(config):
    return config.get("library_path")


def get_playlist_platform(entry):
    """
    Which service this playlist entry lives on natively -- PLATFORM_SPOTIFY
    or PLATFORM_YOUTUBE (Checklist item 6). Falls back to
    PLATFORM_SPOTIFY for any entry missing the field entirely (shouldn't
    happen after load_config()'s backfill, but kept defensive for any
    entry dict built in memory rather than loaded from disk).
    """
    return entry.get("platform", PLATFORM_SPOTIFY)


def get_playlist_source_id(entry):
    """
    The ID of the actual playlist this entry tracks on its native
    platform -- entry['spotify_id'] for a Spotify-native playlist,
    entry['youtube_playlist_id'] for a YouTube-native one (Checklist
    item 6, design Q5, option C). Centralizing this here means calling
    code never has to check platform itself just to read the right key
    -- library_sync.py, exceptions_manager lookups, and the Exceptions
    menu all go through this rather than reading either key directly.
    """
    platform = get_playlist_platform(entry)
    if platform == PLATFORM_YOUTUBE:
        return entry.get("youtube_playlist_id")
    if platform == PLATFORM_YTMUSIC:  # v0.16.51 (Checklist item 42a)
        return entry.get("ytmusic_album_id") if is_album(entry) else entry.get("ytmusic_playlist_id")
    if platform == PLATFORM_SOUNDCLOUD:  # v0.16.52 (Checklist item 42b): playlist or album
        return entry.get("soundcloud_set")
    if platform == PLATFORM_APPLE_MUSIC:  # v0.16.53 (Checklist item 42c)
        return entry.get("applemusic_album") if is_album(entry) else entry.get("applemusic_playlist")
    if platform == PLATFORM_AMAZON_MUSIC:  # v0.16.54 (Checklist item 42d)
        return entry.get("amazonmusic_album") if is_album(entry) else entry.get("amazonmusic_playlist")
    if is_album(entry):
        return entry.get("spotify_album_id")
    return entry.get("spotify_id")


def uses_video_ids(entry):
    """Whether this entry's songs are YouTube videos -- identified by
    "yt:<videoId>", downloaded as listed, exceptions keyed by the bare
    video ID: a YouTube playlist and, since v0.16.51, anything on YT
    Music. Otherwise they are Spotify tracks."""
    return get_playlist_platform(entry) in VIDEO_PLATFORMS


def uses_search(entry):
    """Whether this entry's songs are searched for on YouTube (the tiered
    search) and identified by their catalogue ID: Spotify and, since
    v0.16.53, Apple Music ("am:<song id>")."""
    return get_playlist_platform(entry) in SEARCH_PLATFORMS


def is_soundcloud(entry):
    """Whether this entry tracks a SoundCloud set (v0.16.52): songs
    identified by "sc:<track id>", downloaded from SoundCloud (or found on
    YouTube when SoundCloud only has a preview)."""
    return get_playlist_platform(entry) == PLATFORM_SOUNDCLOUD


def get_source_kind(entry):
    """KIND_ALBUM or KIND_PLAYLIST (Checklist item 41a, v0.16.48). An
    entry without `kind` is a playlist."""
    return KIND_ALBUM if entry.get("kind") == KIND_ALBUM else KIND_PLAYLIST


def is_album(entry):
    """Whether this entry tracks an album (v0.16.48; a Spotify or, since
    v0.16.51, a YT Music album)."""
    return get_source_kind(entry) == KIND_ALBUM


def entry_source(entry):
    """(platform, kind, source_id) of a tracked entry, comparable with
    detect_source()'s result (v0.16.48)."""
    return get_playlist_platform(entry), get_source_kind(entry), get_playlist_source_id(entry)


def source_link(platform, source_id, kind=KIND_PLAYLIST):
    """The web link of a playlist / album source. Moved here from
    ui_main_page in v0.17.6 (Checklist item 57) so the Sync Log, which has
    no Qt, can name its source too; the GUI calls this one."""
    if platform == PLATFORM_YOUTUBE:
        return f"https://www.youtube.com/playlist?list={source_id}"
    if platform == PLATFORM_SOUNDCLOUD:  # v0.16.52 (Checklist item 42b)
        return soundcloud_link(source_id)
    if platform == PLATFORM_AMAZON_MUSIC:  # v0.16.54 (Checklist item 42d)
        import amazon_music_reader
        return amazon_music_reader.page_link(source_id, "album" if kind == KIND_ALBUM else "playlist")
    if platform == PLATFORM_APPLE_MUSIC:  # v0.16.53 (Checklist item 42c)
        import apple_music_reader
        if kind == KIND_ALBUM:
            return apple_music_reader.album_link(source_id)
        return apple_music_reader.playlist_link(source_id)
    if platform == PLATFORM_YTMUSIC:  # v0.16.51 (Checklist item 42a)
        import ytmusic_reader
        if kind == KIND_ALBUM:
            return ytmusic_reader.album_link(source_id)
        return ytmusic_reader.playlist_link(source_id)
    if kind == KIND_ALBUM:  # v0.16.48 (Checklist item 41a)
        return f"https://open.spotify.com/album/{source_id}"
    return f"https://open.spotify.com/playlist/{source_id}"


def entry_link(entry):
    """The web link of a tracked entry's source (v0.17.6)."""
    platform, kind, source_id = entry_source(entry)
    return source_link(platform, source_id, kind)


def _set_entry_source(entry, platform, kind, source_id):
    """Point an entry at (platform, kind, source_id): exactly one ID key,
    and `kind` only on an album (v0.16.48)."""
    for key in ("spotify_id", "spotify_album_id", "youtube_playlist_id", "kind",
                "ytmusic_playlist_id", "ytmusic_album_id",  # v0.16.51
                "soundcloud_set",  # v0.16.52
                "applemusic_playlist", "applemusic_album",  # v0.16.53
                "amazonmusic_playlist", "amazonmusic_album",  # v0.16.54
                "artist_id"):  # v0.16.50: a new link is no longer the artist's release
        entry.pop(key, None)
    entry["platform"] = platform
    if platform == PLATFORM_YOUTUBE:
        entry["youtube_playlist_id"] = source_id
    elif platform == PLATFORM_AMAZON_MUSIC:  # v0.16.54 (Checklist item 42d)
        if kind == KIND_ALBUM:
            entry["kind"] = KIND_ALBUM
            entry["amazonmusic_album"] = source_id
        else:
            entry["amazonmusic_playlist"] = source_id
    elif platform == PLATFORM_APPLE_MUSIC:  # v0.16.53 (Checklist item 42c)
        if kind == KIND_ALBUM:
            entry["kind"] = KIND_ALBUM
            entry["applemusic_album"] = source_id
        else:
            entry["applemusic_playlist"] = source_id
    elif platform == PLATFORM_SOUNDCLOUD:  # v0.16.52 (Checklist item 42b)
        if kind == KIND_ALBUM:
            entry["kind"] = KIND_ALBUM
        entry["soundcloud_set"] = source_id
    elif platform == PLATFORM_YTMUSIC:  # v0.16.51 (Checklist item 42a)
        if kind == KIND_ALBUM:
            entry["kind"] = KIND_ALBUM
            entry["ytmusic_album_id"] = source_id
        else:
            entry["ytmusic_playlist_id"] = source_id
    elif kind == KIND_ALBUM:
        entry["kind"] = KIND_ALBUM
        entry["spotify_album_id"] = source_id
    else:
        entry["spotify_id"] = source_id


def source_label(platform, kind=KIND_PLAYLIST):
    """"Spotify", "Spotify album", "YouTube" (v0.16.48), "YT Music" or "YT
    Music album" (v0.16.51)."""
    if platform == PLATFORM_YOUTUBE:
        return "YouTube"
    name = platform_name(platform)
    return f"{name} album" if kind == KIND_ALBUM else name


def set_library_path(config, new_path):
    """
    Point the app at a new library folder, without moving any existing
    files -- kept for backward compatibility (and for anywhere in the
    app that only ever wants a plain repoint). Delegates to
    change_library_path() with move_files=False, which is exactly the
    original behavior this function has always had.
    """
    return change_library_path(config, new_path, move_files=False)


def change_library_path(config, new_path, move_files=False, on_progress=None):
    """
    Change the Library folder path (Checklist item 4 -- "Music Folder
    path change: move-or-relocate UX").

    move_files=False (the default): identical to the app's original
    behavior -- just repoints the app at new_path going forward. No
    files are touched; anything already downloaded stays exactly where
    it is on disk.

    move_files=True: in addition to repointing, physically moves every
    currently-tracked playlist's folder from the old Library location
    into new_path -- except any playlist with a custom_path, which
    lives outside the Library folder on purpose (via "Connect an
    Already-Downloaded Playlist") and is never touched by a
    Library-path change.

    The move is built directly on Checklist item 3's crash-safety work:
      - Before any folder is touched, a `_pending_library_move`
        breadcrumb recording every folder still to be moved is saved
        to config.
      - Each folder is moved with _do_move_playlist_folder() -- copy
        to a temp name at the new location, atomic os.replace() into
        its final name once the copy is verified complete, then delete
        the original.
      - config["library_path"] itself is only updated to new_path once
        every folder is confirmed moved. If the process is interrupted
        at any point, the app is left pointing at the OLD (still
        completely valid, nothing removed from it yet) location, and
        load_config() automatically resumes and finishes the move on
        the next launch (see _recover_pending_library_move()).

    Raises ValueError if a previous library move is still incomplete
    (rather than starting a second one against a different target,
    which could split a playlist's files across two locations) --
    restart the app to let it self-heal, or call this again pointing
    at the SAME new_path already recorded in the pending move to retry
    whichever folder(s) failed.

    on_progress: optional callback receiving short human-readable
    status strings (same convention as library_sync's on_progress) --
    a no-op if not given.

    Returns the resolved new_path. When move_files=True, compare the
    return value against get_library_path(config) afterward: if they
    don't match, at least one folder failed to move and library_path
    was deliberately left unchanged -- see logs/error_log.txt for the
    specific failure(s).
    """
    def report(message):
        if on_progress:
            on_progress(message)

    existing_pending = config.get("_pending_library_move")
    if existing_pending:
        raise ValueError(
            f"A previous library-folder move to "
            f"'{existing_pending['new_library_path']}' is still incomplete -- some "
            f"playlist folders may not have finished moving yet. Restart the app to let "
            f"it self-heal automatically, or run this again pointing at "
            f"'{existing_pending['new_library_path']}' to retry the folder(s) that "
            f"failed, before choosing a different new location."
        )

    new_path = os.path.abspath(os.path.expanduser(new_path.strip()))
    try:
        os.makedirs(new_path, exist_ok=True)
    except OSError as e:
        logger.log_error(f"Failed to create library folder at {new_path}: {e}")
        raise ValueError(f"Could not create or access that folder: {e}")

    old_path = get_library_path(config)

    if not move_files or not old_path:
        config["library_path"] = new_path
        save_config(config)
        logger.log_event(f"Library folder set to: {new_path}")
        return new_path

    movable_entries = [e for e in config.get("playlists", []) if not e.get("custom_path")]
    if not movable_entries:
        config["library_path"] = new_path
        save_config(config)
        logger.log_event(
            f"Library folder set to: {new_path} (nothing to move -- no tracked playlists "
            f"without a custom_path)"
        )
        return new_path

    pending = {
        "old_library_path": old_path,
        "new_library_path": new_path,
        "folders": {e["folder_name"]: "pending" for e in movable_entries},
    }
    config["_pending_library_move"] = pending
    save_config(config)

    all_ok = True
    for entry in movable_entries:
        folder_name = entry["folder_name"]
        report(f"Moving '{folder_name}'...")
        old_dir = os.path.join(old_path, folder_name)
        new_dir = os.path.join(new_path, folder_name)

        if _do_move_playlist_folder(old_dir, new_dir):
            pending["folders"][folder_name] = "done"
            save_config(config)
            report(f"'{folder_name}' moved.")
        else:
            all_ok = False
            report(f"'{folder_name}' FAILED to move -- see logs/error_log.txt. Left in its original location.")

    if all_ok:
        config["library_path"] = new_path
        config.pop("_pending_library_move", None)
        save_config(config)
        logger.log_event(f"Library folder moved and repointed to: {new_path}")
    else:
        logger.log_error(
            f"Library move to '{new_path}' finished with at least one failed folder -- "
            f"library_path was deliberately left unchanged (still '{old_path}') so the "
            f"app never points at a partially-moved location. Retry to finish the "
            f"remaining folder(s)."
        )

    return new_path


def list_playlists(config):
    return config.get("playlists", [])


def in_albums_section(entry):
    """Whether this entry is listed in the Albums section, after every
    playlist (v0.16.49): today, a Spotify album."""
    return is_album(entry)


def list_artists(config):
    """The artists added from a discography (v0.16.50, artist_watch.py)."""
    return config.get("artists", [])


_ARTIST_KEYS = {PLATFORM_YTMUSIC: "ytmusic_artist_id", PLATFORM_APPLE_MUSIC: "applemusic_artist_id"}


def artist_id_of(artist):
    """A tracked artist's ID: "spotify_artist_id", or for a YT Music
    artist (v0.16.51) "ytmusic_artist_id", an Apple Music one (v0.16.53)
    "applemusic_artist_id"."""
    return (artist.get("spotify_artist_id") or artist.get("ytmusic_artist_id")
            or artist.get("applemusic_artist_id"))


def artist_platform(artist):
    """PLATFORM_SPOTIFY, (v0.16.51) PLATFORM_YTMUSIC or (v0.16.53)
    PLATFORM_APPLE_MUSIC."""
    if artist.get("platform"):
        return artist["platform"]
    return next((p for p, key in _ARTIST_KEYS.items() if artist.get(key)), PLATFORM_SPOTIFY)


def new_artist(platform, artist_id, **fields):
    """A new artist dict for config["artists"] (v0.16.51): Spotify ones
    keep exactly the v0.16.50 shape; others add "platform" and their own
    ID key."""
    if platform in _ARTIST_KEYS:
        return {"platform": platform, _ARTIST_KEYS[platform]: artist_id, **fields}
    return {"spotify_artist_id": artist_id, **fields}


def find_artist(config, artist_id):
    for artist in config.get("artists", []):
        if artist_id_of(artist) == artist_id:
            return artist
    return None


def entry_artist_id(config, entry):
    """The artist an album belongs to (v0.16.50), if that artist is still
    tracked, else None."""
    artist_id = entry.get("artist_id") if in_albums_section(entry) else None
    return artist_id if artist_id and find_artist(config, artist_id) else None


def section_key(config, entry):
    """Which list an entry is shown in (v0.16.49 / v0.16.50): ("playlist",),
    ("artist", artist id) for an album of a tracked artist, or ("album",)
    for an album added on its own."""
    if not in_albums_section(entry):
        return ("playlist",)
    artist_id = entry_artist_id(config, entry)
    return ("artist", artist_id) if artist_id else ("album",)


def _section_rank(config):
    """section key -> its place: playlists, then each artist's albums in
    the order the artists were added, then the albums added on their own."""
    ranks = {("playlist",): 0}
    for i, artist in enumerate(config.get("artists", []), start=1):
        ranks[("artist", artist_id_of(artist))] = i
    ranks[("album",)] = len(ranks)
    return ranks


def group_playlists(config):
    """Keep the list in its sections, playlists first, then albums
    (v0.16.49), without changing the order inside any one. Since v0.16.50
    the albums are grouped by artist (each tracked artist's albums
    together, in the order the artists were added), then the albums added
    on their own. Done in memory on every load and after anything that
    adds an entry or changes its kind, so the stored order follows at the
    next save. Returns True if anything moved."""
    playlists = config.get("playlists", [])
    ranks = _section_rank(config)
    grouped = sorted(playlists, key=lambda e: ranks.get(section_key(config, e), len(ranks)))
    if grouped == playlists:
        return False
    playlists[:] = grouped
    return True


def section_range(config, entry):
    """(first, last) 1-based positions of the section this entry is in
    (v0.16.49; v0.16.50: an artist's albums are a section of their own).
    An entry can only be moved within its own section."""
    playlists = list_playlists(config)
    key = section_key(config, entry)
    positions = [i for i, e in enumerate(playlists, start=1) if section_key(config, e) == key]
    return (positions[0], positions[-1]) if positions else (1, len(playlists))


def get_playlist_folder(config, entry) -> Path:
    """
    Resolve the actual folder Path for a tracked playlist entry.
    Normally this is <library_path>/<folder_name>, but if the entry has
    a custom_path (set via "Connect an already-downloaded playlist"),
    that exact absolute path is used instead -- letting a playlist's
    files live anywhere on disk, not just inside the managed Library
    folder.
    """
    custom_path = entry.get("custom_path")
    if custom_path:
        return Path(custom_path)

    library_path = get_library_path(config)
    if not library_path:
        raise ValueError("No library folder is set yet.")
    return Path(library_path) / entry["folder_name"]


def find_playlist(config, folder_name):
    for entry in config.get("playlists", []):
        if entry["folder_name"].lower() == folder_name.lower():
            return entry
    return None


def add_playlist(config, source_input, folder_name, custom_path=None, artist_id=None, kind=None):
    """
    Start tracking a playlist. source_input can be a Spotify playlist
    URL/ID OR a YouTube playlist URL/ID (Checklist item 6, design Q6)
    -- which platform it belongs to is detected automatically, tried
    in this order: Spotify first, then YouTube. There is no separate
    "add a YouTube playlist" menu path; "Download & Sync It" and
    "Connect an Already-Downloaded Playlist" both accept either kind
    of link transparently. folder_name is the display name used
    everywhere in the app, and (when custom_path is not given) is also
    the literal folder name created under the Library path.

    custom_path: optional. If given, this exact existing folder is used
    as the playlist's storage location instead of creating a new one
    under the Library path -- for "Connect an already-downloaded
    playlist" whose files already live somewhere else on disk. That
    folder must already exist; it is not created here. The playlist's
    display name (folder_name) stays independent of this real path --
    renaming the playlist later will not move or rename this folder.

    Raises ValueError if source_input isn't recognizable as either a
    Spotify or a YouTube playlist link/ID.

    v0.16.48 (Checklist item 41a): a Spotify album link is accepted too
    (detect_source(): album, then playlist, then YouTube) and tracked as
    an album entry (`kind` "album", `spotify_album_id`).

    kind (v0.16.52, Checklist item 42b): KIND_ALBUM for a SoundCloud set
    the caller looked up and found to be an album (a set link doesn't say).
    Ignored for every other platform, whose links say what they are.
    """
    folder_name = folder_name.strip()
    if not folder_name:
        raise ValueError("Folder name cannot be empty.")

    detected = detect_source(source_input)
    if not detected:
        raise ValueError(unrecognised_source_message(source_input))
    platform, detected_kind, source_id = detected
    kind = kind if (platform == PLATFORM_SOUNDCLOUD and kind in (KIND_ALBUM, KIND_PLAYLIST)) else detected_kind
    detected = (platform, kind, source_id)

    if find_playlist(config, folder_name):
        raise ValueError(f"A tracked playlist already uses the name '{folder_name}'.")

    for existing_entry in config.get("playlists", []):
        if same_source(entry_source(existing_entry), detected):
            what = "album" if kind == KIND_ALBUM else "playlist"
            raise ValueError(
                f"That {what} is already tracked as '{existing_entry['folder_name']}'."
            )

    entry = {"folder_name": folder_name}
    _set_entry_source(entry, platform, kind, source_id)
    if artist_id and kind == KIND_ALBUM:
        entry["artist_id"] = artist_id  # v0.16.50: one of a tracked artist's releases

    if custom_path:
        resolved_path = os.path.abspath(os.path.expanduser(str(custom_path).strip()))
        if not os.path.isdir(resolved_path):
            raise ValueError(f"That folder doesn't exist: {resolved_path}")
        entry["custom_path"] = resolved_path
    else:
        # Create the playlist's folder right away if the library path is already set
        library_path = get_library_path(config)
        if library_path:
            try:
                os.makedirs(os.path.join(library_path, folder_name), exist_ok=True)
            except OSError as e:
                logger.log_error(f"Failed to create playlist folder '{folder_name}': {e}")

    config.setdefault("playlists", []).append(entry)
    group_playlists(config)  # v0.16.49: a new playlist goes before the albums
    save_config(config)
    logger.log_event(
        f"Added tracked {'album' if kind == KIND_ALBUM else 'playlist'} '{folder_name}' ({platform}, ID: {source_id})"
        + (f" -> linked to existing folder {entry.get('custom_path')}" if custom_path else "")
    )

    return entry


def remove_playlist(config, folder_name):
    """
    Stop tracking a playlist. Does NOT delete the local folder or its
    files -- it only removes the entry from the tracked list.
    """
    entry = find_playlist(config, folder_name)
    if not entry:
        raise ValueError(f"No tracked playlist found with folder name '{folder_name}'.")

    config["playlists"] = [
        e for e in config["playlists"] if e["folder_name"].lower() != folder_name.lower()
    ]
    save_config(config)
    logger.log_event(f"Stopped tracking playlist '{folder_name}' (files were left in place)")
    return entry


def rename_playlist(config, old_name, new_name):
    """
    Rename a tracked playlist's display name. If it does NOT have a
    custom_path (its folder lives under the Library path the normal
    way), the actual directory on disk is renamed to match, so the two
    never drift apart. If it DOES have a custom_path (an already-
    downloaded playlist linked from elsewhere), only the display name
    changes -- the real folder's location and name on disk are left
    completely untouched. Behavior is identical for a Spotify- or
    YouTube-native entry.

    Checklist item 3: renaming the real folder on disk and saving the
    updated config are two separate steps, so a `_pending_folder_rename`
    breadcrumb is saved to config *before* the disk rename happens.
    If the process is killed between the disk rename and the final
    config save below, load_config() detects and finishes the
    interrupted rename automatically the next time the app starts
    (see _recover_pending_folder_rename()).
    """
    entry = find_playlist(config, old_name)
    if not entry:
        raise ValueError(f"No tracked playlist found named '{old_name}'.")

    new_name = new_name.strip()
    if not new_name:
        raise ValueError("New name cannot be empty.")
    if new_name.lower() != old_name.lower() and find_playlist(config, new_name):
        raise ValueError(f"A tracked playlist already uses the name '{new_name}'.")

    if not entry.get("custom_path"):
        library_path = get_library_path(config)
        if library_path:
            old_dir = os.path.join(library_path, entry["folder_name"])
            new_dir = os.path.join(library_path, new_name)
            if os.path.isdir(old_dir):
                config["_pending_folder_rename"] = {
                    "old_folder_name": entry["folder_name"],
                    "new_folder_name": new_name,
                    "old_dir": old_dir,
                    "new_dir": new_dir,
                }
                save_config(config)
                try:
                    os.rename(old_dir, new_dir)
                except OSError as e:
                    config.pop("_pending_folder_rename", None)
                    save_config(config)
                    logger.log_error(f"Failed to rename folder '{old_dir}' to '{new_dir}': {e}")
                    raise ValueError(f"Could not rename the folder on disk: {e}")

    entry["folder_name"] = new_name
    config.pop("_pending_folder_rename", None)
    save_config(config)
    logger.log_event(f"Renamed tracked playlist '{old_name}' -> '{new_name}'")
    return entry


def update_playlist_source_id(config, folder_name, new_source_input, allow_platform_change=False):
    """
    Repoint a tracked playlist at a different playlist on the SAME
    platform it already tracks (Checklist item 6, v0.15.0 -- supersedes
    update_playlist_spotify_id(), kept below as a backward-compatible
    alias). By default, switching a playlist's platform isn't supported
    here -- remove it and re-add it with the new link instead, since
    that's a different-enough operation (different identity-key scheme,
    likely different metadata available) that silently converting in
    place would risk hiding what actually changed.

    allow_platform_change=True (Checklist item 16, v0.16.12 -- the GUI's
    Edit page, which shows its own warning and requires the playlist's
    name to be typed first): the new link may be on either platform. It
    is detected like add_playlist() does; on a Spotify <-> YouTube
    switch the entry's `platform` is updated and its ID key swapped
    (the old key is removed, so the entry never carries both). A link
    that another tracked playlist already uses is refused (ValueError),
    and a link to the playlist it already tracks changes nothing.

    WARNING (surface this to the user before calling): existing local
    files were matched against the OLD playlist's tracks/videos. After
    this change, none of them will match the new playlist's live
    tracks, so the next sync will treat every existing file as "no
    longer live" -- removing all of them -- before downloading the new
    playlist's tracks fresh. This function itself does not touch any
    files; it only updates which playlist is tracked. Saved exceptions
    stay in exceptions.json under the old playlist's ID (they would
    apply again if the old link were restored).
    """
    entry = find_playlist(config, folder_name)
    if not entry:
        raise ValueError(f"No tracked playlist found named '{folder_name}'.")

    if allow_platform_change:
        # v0.16.48 (Checklist item 41a): a playlist <-> album switch too.
        detected = detect_source(new_source_input)
        if not detected:
            raise ValueError(unrecognised_source_message(new_source_input))
        old = entry_source(entry)
        if detected[0] == PLATFORM_SOUNDCLOUD:
            # v0.16.52: a set link doesn't say whether it's an album; another
            # SoundCloud set keeps the entry's kind, anything else is a playlist.
            detected = (detected[0], old[1] if old[0] == PLATFORM_SOUNDCLOUD else KIND_PLAYLIST, detected[2])
        if same_source(detected, old):
            return entry
        for other in config.get("playlists", []):
            if other is not entry and same_source(entry_source(other), detected):
                raise ValueError(f"That {'album' if detected[1] == KIND_ALBUM else 'playlist'} "
                                 f"is already tracked as '{other['folder_name']}'.")

        _set_entry_source(entry, *detected)
        group_playlists(config)  # v0.16.49: a kind change moves it to its section
        save_config(config)
        logger.log_event(
            f"Changed {old[1]} tracked by '{folder_name}': {old[0]} {old[2]} -> "
            f"{detected[0]} {detected[1]} {detected[2]}"
        )
        return entry

    platform = get_playlist_platform(entry)
    if platform in (PLATFORM_APPLE_MUSIC, PLATFORM_AMAZON_MUSIC):
        # v0.16.53 / v0.16.54 (Checklist items 42c / 42d): another link of
        # the same platform and kind.
        album = is_album(entry)
        apple = platform == PLATFORM_APPLE_MUSIC
        extract = ((extract_apple_music_album if album else extract_apple_music_playlist) if apple
                   else (extract_amazon_music_album if album else extract_amazon_music_playlist))
        new_id = extract(new_source_input)
        if not new_id:
            raise ValueError(f"Could not recognize that as an {platform_name(platform)} "
                             f"{'album' if album else 'playlist'} link.")
        key = ("applemusic_" if apple else "amazonmusic_") + ("album" if album else "playlist")
        old_id = entry.get(key)
        entry[key] = new_id
        if new_id != old_id:
            entry.pop("artist_id", None)
    elif platform == PLATFORM_SOUNDCLOUD:
        # v0.16.52 (Checklist item 42b): another SoundCloud set; the kind stays.
        new_id = extract_soundcloud_set(new_source_input)
        if not new_id:
            raise ValueError("Could not recognize that as a SoundCloud playlist or album link "
                             "(soundcloud.com/…/sets/…).")
        old_id = entry.get("soundcloud_set")
        entry["soundcloud_set"] = new_id
    elif platform == PLATFORM_YTMUSIC:
        # v0.16.51 (Checklist item 42a): another YT Music link of the same kind.
        album = is_album(entry)
        new_id = (extract_ytmusic_album_id if album else extract_ytmusic_playlist_id)(new_source_input)
        if not new_id:
            raise ValueError(f"Could not recognize that as a YT Music {'album' if album else 'playlist'} link.")
        key = "ytmusic_album_id" if album else "ytmusic_playlist_id"
        old_id = entry.get(key)
        entry[key] = new_id
        if new_id != old_id:
            entry.pop("artist_id", None)
    elif is_album(entry):
        # v0.16.48: an album entry's link can only be changed to another album here.
        new_id = extract_spotify_album_id(new_source_input)
        if not new_id:
            raise ValueError("Could not recognize that as a Spotify album link.")
        old_id = entry.get("spotify_album_id")
        entry["spotify_album_id"] = new_id
        if new_id != old_id:
            entry.pop("artist_id", None)  # v0.16.50: no longer the artist's release
    elif platform == PLATFORM_YOUTUBE:
        new_id = extract_youtube_playlist_id(new_source_input)
        if not new_id:
            raise ValueError("Could not recognize that as a YouTube playlist link or ID.")
        old_id = entry.get("youtube_playlist_id")
        entry["youtube_playlist_id"] = new_id
    else:
        new_id = extract_playlist_id(new_source_input)
        if not new_id:
            raise ValueError("Could not recognize that as a Spotify playlist link or ID.")
        old_id = entry.get("spotify_id")
        entry["spotify_id"] = new_id

    save_config(config)
    logger.log_event(f"Changed {platform} playlist tracked by '{folder_name}': {old_id} -> {new_id}")
    return entry


def update_playlist_spotify_id(config, folder_name, new_spotify_input):
    """
    Backward-compatible alias for update_playlist_source_id() -- kept
    under its original name for anything written against it before
    v0.15.0. Works the same regardless of which name is used.
    """
    return update_playlist_source_id(config, folder_name, new_spotify_input)


def reorder_playlist(config, folder_name, new_position):
    """
    Move a tracked playlist to a new 1-based position in the list,
    changing the order (and therefore the numbers) it's shown in on
    the main menu. Purely a list-order change -- does not touch the
    playlist's folder, files, source ID, or anything else.

    v0.16.49: an entry stays in its own section (playlists, then albums),
    so a position outside it is refused (section_range()).
    """
    playlists = config.get("playlists", [])
    entry = find_playlist(config, folder_name)
    if not entry:
        raise ValueError(f"No tracked playlist found named '{folder_name}'.")

    group_playlists(config)
    first, last = section_range(config, entry)
    if not (first <= new_position <= last):
        key = section_key(config, entry)
        what = {"playlist": "Playlists", "artist": "An artist's albums"}.get(key[0], "Albums")
        raise ValueError(f"{what} can only be moved between positions {first} and {last}.")

    playlists.remove(entry)
    playlists.insert(new_position - 1, entry)
    save_config(config)
    logger.log_event(f"Reordered '{folder_name}' to position {new_position}")
    return entry
