"""
Mud Music Manager
Entry point.

Copyright (c) 2026 Michael Xuereb.
Free software under the GNU General Public License v3 or later (see
LICENSE.txt); third-party libraries are listed in THIRD_PARTY_NOTICES.txt.

As of v0.16.0 the app opens its graphical interface (ui_app.py, PySide6)
by default. The classic terminal menu below is unchanged in spirit and is
the fail-safe route: run `python main.py --cli` to use it directly, it is
what the GUI's CMD button opens, and it is used automatically if the GUI
can't start (e.g. PySide6 isn't installed yet).

As of v0.16.3 a double-click on main.py no longer leaves a console window
open: on Windows the GUI relaunches itself under pythonw.exe (no console),
and Settings -> "Open Debug Window" opens a real console on demand. The
console plumbing lives in debug_console.py; `--cli` keeps its console.

See CHANGELOG.md for version history.
"""

import os
import re
import signal
import sys

import debug_console

# Must run before the heavy imports below. When started by double-clicking
# (Windows), restart under pythonw.exe so no console window stays open, then
# exit this console-owning copy. Does nothing for `--cli`, from a terminal
# you opened yourself, or if anything looks off (see debug_console.py).
if __name__ == "__main__" and "--cli" not in sys.argv[1:]:
    if debug_console.relaunch_without_console_if_owned(os.path.abspath(__file__), sys.argv[1:]):
        sys.exit(0)
    debug_console.install_early_excepthook()

from datetime import datetime
from pathlib import Path

from colorama import init as colorama_init, Fore, Style

import config_manager
import acquisition_engine
import app_info
import artist_watch
import library_sync
import exceptions_manager
import dependency_checker
import download_settings
import logger
import metadata_tagger
import track_failures
import ui_libraries
import ui_settings
import user_data
import youtube_cookies

VERSION = "0.17.9"

# Enables ANSI color codes to render correctly on plain Windows
# cmd.exe, not just Windows Terminal/PowerShell (which already support
# ANSI natively) -- needed for the platform indicator below (Checklist
# item 6, v0.14.0). A no-op on Mac/Linux terminals. Skipped when there is no
# stdout at all (pythonw.exe, v0.16.3).
if sys.stdout is not None:
    colorama_init()


# ---------------------------------------------------------------------------
# Keyword menu dispatch
#
# Numbers are ONLY used to select a playlist (or a track, or a saved
# exception) from a list. Every action menu (which thing to DO) is
# keyword-based and case-insensitive, per Michael's request.
# ---------------------------------------------------------------------------

MAIN_MENU_ACTIONS = {
    "add": ["add"],
    "edit": ["edit"],
    "remove": ["remove"],
    "sync": ["sync"],
    "scan": ["scan"],
    "check": ["check integrity", "check", "integrity", "check for issues"],
    "change_path": ["change music folder path", "change", "path", "folder"],
    "update": ["update project libraries", "update"],
    "settings": ["settings", "setting"],
    "done": ["done"],
}

SETTINGS_MENU_ACTIONS = {
    "theme": ["theme", "appearance", "dark", "light"],
    "cmd": ["cmd", "cmd button", "command"],
    "download": ["download quality", "download", "quality"],
    "explicit": ["explicit", "prefer explicit", "prefer explicit versions"],  # v0.17.7
    "signin": ["youtube sign-in", "sign-in", "sign in", "signin", "youtube", "cookies"],
    "libraries": ["libraries", "library", "paths"],
    "export": ["export", "export data", "export your data", "backup"],  # v0.17.9 (Checklist item 59)
    "import": ["import", "import data", "import your data", "restore"],  # v0.17.9
    "done": ["done"],
}

# v0.16.47 (Checklist item 40): Settings -> Download quality.
DOWNLOAD_MENU_ACTIONS = {
    "reset": ["reset", "reset to defaults", "defaults"],
    "done": ["done"],
}

# v0.16.47 (Checklist item 40): Settings -> YouTube sign-in.
SIGNIN_MENU_ACTIONS = {
    "off": ["off"],
    "browser": ["browser"],
    "file": ["cookies.txt", "cookies", "file"],
    "test": ["test"],
    "done": ["done"],
}

ADD_MENU_ACTIONS = {
    "download": ["download & sync it", "download and sync it", "download", "sync it"],
    "connect": ["connect an already-downloaded playlist", "connect"],
    "done": ["done"],
}

EDIT_MENU_ACTIONS = {
    "name": ["name"],
    "url": ["url"],
    "reorder": ["reorder", "order", "move"],
    "exceptions": ["exceptions"],
    "logs": ["logs", "log", "sync log", "sync logs"],
    "done": ["done"],
}

EXCEPTIONS_MENU_ACTIONS = {
    "fix": ["fix", "add", "add / fix a track's match", "fix a track's match"],
    "remove": ["remove"],
    "done": ["done"],
}

# Platform indicator (Checklist item 6, v0.14.0): a short colored tag
# shown to the left of a playlist's name everywhere it's listed, per
# Michael's request -- a stand-in for platform-logo images until a
# real UI exists. As of v0.15.0 a YouTube-native playlist can actually
# be added (Add -> Download & Sync It accepts either kind of link), so
# red YT now appears for those.
PLATFORM_TAGS = {
    config_manager.PLATFORM_SPOTIFY: (Fore.GREEN, "SPT"),
    config_manager.PLATFORM_YOUTUBE: (Fore.RED, "YT"),
    config_manager.PLATFORM_YTMUSIC: (Fore.LIGHTRED_EX, "YTM"),  # v0.16.51 (Checklist item 42a)
    config_manager.PLATFORM_SOUNDCLOUD: (Fore.YELLOW, "SC"),     # v0.16.52 (Checklist item 42b)
    config_manager.PLATFORM_APPLE_MUSIC: (Fore.LIGHTMAGENTA_EX, "AM"),  # v0.16.53 (Checklist item 42c)
    config_manager.PLATFORM_AMAZON_MUSIC: (Fore.CYAN, "AMZ"),           # v0.16.54 (Checklist item 42d)
}


def _platform_tag(entry):
    """Short colored platform indicator for a playlist entry -- green
    'SPT' for Spotify-native, red 'YT' for YouTube-native. Falls back
    to a plain, uncolored '?' for anything unrecognized, rather than
    guessing which color it should be."""
    platform = config_manager.get_playlist_platform(entry)
    color, label = PLATFORM_TAGS.get(platform, (None, "?"))
    if color is None:
        return label
    tag = f"{color}{label}{Style.RESET_ALL}"
    if config_manager.is_album(entry):
        # v0.16.48 (Checklist item 41a): the album marker, as the GUI's album icon.
        tag += f" {Fore.MAGENTA}ALB{Style.RESET_ALL}"
    return tag


def _album_name_if_blank(source_input, name):
    """The name typed, or for an album link with no name typed, the album's
    "Artist - Album" (v0.16.48, Checklist item 41a; a YT Music album too
    since v0.16.51). Returns "" if it can't be looked up (add_playlist()
    then says the name is missing)."""
    if name or not config_manager.extract_album_id(source_input):
        return name
    print("Looking up the album's name...")
    try:
        info = acquisition_engine.get_album_info(config_manager.extract_album_id(source_input))
    except Exception as e:
        print(f"Couldn't look the album up ({e}). Please type a name.")
        return ""
    print(f"Using the name '{info.folder_name}'.")
    return info.folder_name


def _soundcloud_lookup(source_input, name):
    """v0.16.52 (Checklist item 42b): for a SoundCloud set or short link,
    look it up -- whether it's an album (a set link doesn't say), its full
    link (a short link is followed) and, for an album with no name typed,
    "Artist - Album". Returns (source, name, kind); anything else, or a
    failed lookup of a full link, comes back as typed (kind None: a
    playlist)."""
    if not (config_manager.extract_soundcloud_set(source_input)
            or config_manager.is_soundcloud_short_link(source_input)):
        return source_input, name, None
    print("Looking the SoundCloud link up...")
    try:
        info = acquisition_engine.get_soundcloud_set_info(source_input)
    except Exception as e:
        print(f"Couldn't look it up ({e}); it's added as a playlist." if
              config_manager.extract_soundcloud_set(source_input) else f"Couldn't look it up ({e}).")
        return source_input, name, None
    what = "album" if info.is_album else "playlist"
    print(f"SoundCloud {what}: {info.name} by {info.primary_artist} ({info.total_tracks} tracks).")
    if not name and info.is_album:
        name = info.folder_name
        print(f"Using the name '{name}'.")
    return info.link, name, info.kind


def _album_source(source_input):
    """v0.16.51: a YT Music album pasted as its own playlist (OLAK5uy_...)
    is added by its album link (looked up), as the artist picker adds it,
    so the two are recognised as the same album. Anything else, or a
    failed lookup, is added as typed."""
    album_id = config_manager.extract_ytmusic_album_id(source_input)
    if not album_id or not album_id.startswith("OLAK5uy_"):
        return source_input
    try:
        return acquisition_engine.get_album_info(album_id).link
    except Exception:
        return source_input


def _match_keyword(user_input, keyword_map):
    """Case-insensitive lookup of user_input against every alias in
    keyword_map. Returns the canonical action name, or None."""
    text = user_input.strip().lower()
    for canonical, aliases in keyword_map.items():
        if text in aliases:
            return canonical
    return None


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _parse_selection(text, max_value):
    """
    Parse a selection string like "1, 4, 7, 8-11, 6" into a sorted list
    of unique valid indices (1-based, within 1..max_value). Invalid
    pieces are silently skipped rather than raising, so a typo in one
    part of the list doesn't throw away the rest of a valid selection.
    """
    indices = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, _, end_str = part.partition("-")
            start_str, end_str = start_str.strip(), end_str.strip()
            if start_str.isdigit() and end_str.isdigit():
                start, end = int(start_str), int(end_str)
                if start <= end:
                    indices.update(range(start, end + 1))
        elif part.isdigit():
            indices.add(int(part))

    return sorted(i for i in indices if 1 <= i <= max_value)


def _print_playlists(config):
    playlists = config_manager.list_playlists(config)
    if not playlists:
        print("\nNo playlists are being actively synced yet.")
        return playlists

    print("\nActively synced playlists:")
    in_albums = False
    section = None
    for i, entry in enumerate(playlists, start=1):
        # v0.16.49: albums are listed separately, after every playlist
        # (config_manager keeps that order, so the numbers run on).
        if config_manager.in_albums_section(entry) and not in_albums:
            in_albums = True
            print("\nAlbums & discographies:")
        # v0.16.50: each artist's albums under the artist's name.
        key = config_manager.section_key(config, entry)
        if key != section and key[0] == "artist":
            artist = config_manager.find_artist(config, key[1]) or {}
            watching = "watching for new releases" if artist.get("watch") else "not watching"
            print(f"  {Fore.MAGENTA}{artist.get('name', '?')}{Style.RESET_ALL} (artist, {watching}):")
        elif key != section and key[0] == "album" and section and section[0] == "artist":
            print("  Albums on their own:")
        section = key
        print(f"{i}.     [{_platform_tag(entry)}] {entry['folder_name']}")
    return playlists


def _select_tracked_playlist(config, prompt="Which playlist? (or 'done'): "):
    """Show tracked playlists numbered, ask the user to pick one.
    Returns the playlists.json entry, or None if there's nothing
    tracked, or the user typed 'done'/an invalid choice."""
    playlists = _print_playlists(config)
    if not playlists:
        return None

    choice = input(f"\n{prompt}").strip()
    if not choice or choice.lower() == "done":
        return None
    if not choice.isdigit() or not (1 <= int(choice) <= len(playlists)):
        print("Not a valid choice.")
        return None

    return playlists[int(choice) - 1]


def _select_track(tracks):
    """Show live tracks numbered, ask the user to pick one. Returns the
    Track object, or None if the choice wasn't valid."""
    print(f"\nTracks in this playlist ({len(tracks)}):")
    for t in tracks:
        print(f"  {t.list_position}) {t.name} - {t.primary_artist}")

    choice = input("Track number (or 'done'): ").strip()
    if not choice or choice.lower() == "done":
        return None

    matches = [t for t in tracks if str(t.list_position) == choice]
    if not matches:
        print("Not a valid track number.")
        return None

    return matches[0]


def _require_library_path(config):
    library_path = config_manager.get_library_path(config)
    if not library_path:
        print("\nNo library folder is set yet -- set one first (type 'change' from the main menu).")
        return None
    return library_path


def _dependency_status_line(dep_statuses):
    """
    Human-readable status for the "Update Project Libraries" menu line.
    Per Checklist item 1: prefixed with a quick-glance emoji so Michael
    doesn't have to read the text to know whether anything needs
    attention -- âœ… when everything's current, âœ— when at least one
    tracked package has a newer version available.
    """
    outdated = [s for s in dep_statuses if s.outdated]
    if outdated:
        names = ", ".join(s.name for s in outdated)
        return f"âœ— {len(outdated)} update(s) available: {names}"
    return "âœ… All up to date."


def _pick_folder_dialog(title="Select the Music Library folder"):
    """
    Try to open a native OS folder-picker dialog via tkinter. Returns
    the selected path, or None if tkinter isn't available, the dialog
    couldn't open (e.g. no display), or nothing was selected -- callers
    should fall back to manual text entry in any of those cases.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None

    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title=title)
        root.destroy()
    except Exception as e:
        logger.log_error(f"Folder picker dialog failed: {e}")
        return None

    return selected or None


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------

def handle_add(config):
    while True:
        print("\nAdd a playlist:")
        print("\tDownload & Sync It | Connect an Already-Downloaded Playlist")
        print("\nDone")
        choice = input("> ").strip()
        action = _match_keyword(choice, ADD_MENU_ACTIONS)

        if action == "download":
            _handle_add_download_and_sync(config)
        elif action == "connect":
            _handle_add_connect_existing(config)
        elif action == "done":
            return
        else:
            print("Not a valid option.")


def _handle_add_download_and_sync(config):
    # Accepts a Spotify OR a YouTube playlist link/ID -- which one it
    # is gets detected automatically inside add_playlist() (Checklist
    # item 6, v0.15.0), so there's no separate menu path per platform.
    # v0.16.48 (Checklist item 41a): a Spotify album link works too.
    source_input = input("Spotify, YT Music or Apple Music playlist / album / artist, Amazon Music or "
                         "SoundCloud playlist / album, or YouTube playlist link (or ID): ").strip()
    artist_id = config_manager.extract_artist_id(source_input)
    if artist_id and not config_manager.extract_album_id(source_input):
        _handle_add_artist(config, artist_id)  # v0.16.50 (Checklist item 41b)
        return
    name = input("Name (for an album, Enter = \"Artist - Album\"): ").strip()
    source_input, name, kind = _soundcloud_lookup(source_input, name)  # v0.16.52
    name = _album_name_if_blank(source_input, name)

    try:
        entry = config_manager.add_playlist(config, _album_source(source_input), name, kind=kind)
    except ValueError as e:
        print(f"\nCouldn't add that playlist: {e}")
        return

    print(f"\nAdded '{entry['folder_name']}' [{_platform_tag(entry)}].")

    if not _require_library_path(config):
        print("Set a library folder, then use Sync to download it.")
        return

    print("\nStarting the initial download...")
    folder = config_manager.get_playlist_folder(config, entry)
    _run_sync(config, entry, folder)


def _handle_add_artist(config, artist_id):
    """v0.16.50 (Checklist items 41b / 41c): Add with a Spotify artist link.
    Lists the artist's albums and singles & EPs, numbered; Enter adds them
    all (Michael's default), or ranges pick some. Then asks whether to
    keep watching the artist (Enter = yes), adds the chosen releases as
    albums ("Artist - Release") and syncs them one after another."""
    print(f"Reading the artist from {artist_watch.platform_name(artist_id)}...")  # v0.16.51: or YT Music
    try:
        info = artist_watch.read_artist(artist_id)
    except RuntimeError as e:
        print(f"\nCouldn't read that artist: {e}")
        return
    tracked = artist_watch.tracked_albums(config)
    artist = config_manager.find_artist(config, info.artist_id)
    known = set((artist or {}).get("known_releases") or [])
    print(f"\n{info.name}:")
    section = None
    for n, r in enumerate(info.releases, start=1):
        if r.section != section:
            section = r.section
            print("  Albums:" if section == "albums" else "  Singles & EPs:")
        note = f"  (already added as '{tracked[r.album_id]['folder_name']}')" if r.album_id in tracked else (
            "  (skipped before)" if r.album_id in known else "")
        bits = " Â· ".join([r.type_text] + ([r.year] if r.year else [])
                          + ([f"{r.total_tracks} tracks"] if r.total_tracks else []))  # YT Music gives no count
        print(f"{n}.  {r.name}  [{bits}]{note}")
    free = [n for n, r in enumerate(info.releases, start=1) if r.album_id not in tracked]
    default = [n for n in free if info.releases[n - 1].album_id not in known]
    choice = input(f"\nWhich to add? (Enter = the {len(default)} not added or skipped before; "
                   "e.g. 1, 4, 7-9; 'none' to add none; 'done' to cancel): ").strip().lower()
    if choice == "done":
        print("Cancelled -- nothing added.")
        return
    if choice == "none":
        picked = []
    elif not choice:
        picked = default
    else:
        picked = [n for n in _parse_selection(choice, len(info.releases)) if n in free]
    watch = input("Keep watching this artist, adding and downloading new releases automatically? "
                  "(Y/n): ").strip().lower() not in ("n", "no")
    chosen = [info.releases[n - 1] for n in picked]
    artist_watch.remember_artist(config, info, watch, [r.album_id for r in info.releases])
    names = artist_watch.plan_folder_names(config, info.name, chosen)
    added = []
    for r in chosen:
        try:
            added.append(config_manager.add_playlist(config, r.link, names[r.album_id],
                                                     artist_id=info.artist_id))
        except ValueError as e:
            print(f"Couldn't add '{r.name}': {e}")
    print(f"\nAdded {info.name} ({'watching' if watch else 'not watching'}) and "
          f"{len(added)} release(s).")
    if not added or not _require_library_path(config):
        return
    for n, entry in enumerate(added, start=1):
        print(f"\n=== Syncing '{entry['folder_name']}' ({n}/{len(added)}) ===")
        if not _run_sync(config, entry, config_manager.get_playlist_folder(config, entry)):
            print(f"The other {len(added) - n} release(s) were added but not synced; Sync finishes them.")
            return


def _handle_add_connect_existing(config):
    source_input = input("Spotify, YT Music, Apple Music, Amazon Music or SoundCloud playlist / album, or "
                         "YouTube playlist link (or ID): ").strip()
    name = input("Name (for an album, Enter = \"Artist - Album\"): ").strip()
    source_input, name, kind = _soundcloud_lookup(source_input, name)  # v0.16.52
    name = _album_name_if_blank(source_input, name)
    existing_path = input("Full path to the existing folder with your downloaded songs: ").strip()

    try:
        entry = config_manager.add_playlist(config, _album_source(source_input), name,
                                            custom_path=existing_path, kind=kind)
    except ValueError as e:
        print(f"\nCouldn't add that playlist: {e}")
        return

    print(f"\nConnected '{entry['folder_name']}' -> {entry['custom_path']}")
    print("Running a sync now to catch anything new or changed...")
    folder = config_manager.get_playlist_folder(config, entry)
    _run_sync(config, entry, folder)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

class _SyncStopped(Exception):
    """Raised at the next safe point of a sync after Ctrl+C (v0.16.47)."""


# library_sync's progress line just before a track's download starts: the
# same safe point the GUI's Cancel uses (the previous track is fully done).
_DOWNLOAD_START_RE = re.compile(r"^\[\d+/\d+\]\s+Downloading '")


class _StopOnCtrlC:
    """v0.16.47 (Checklist item 40): while a sync runs, the first Ctrl+C
    asks it to stop before the next download, like the GUI's Cancel (the
    current download finishes); a second Ctrl+C stops at once, and the
    menu loop takes it back to the main menu (files stay safe, Â§3.8).
    The previous Ctrl+C handler is put back afterwards."""

    def __init__(self):
        self.requested = False
        self._installed = False
        self._previous = None

    def __enter__(self):
        try:
            self._previous = signal.signal(signal.SIGINT, self._handle)
            self._installed = True
        except ValueError:  # not the main thread: leave Ctrl+C as it is
            pass
        return self

    def __exit__(self, *exc):
        if self._installed:
            signal.signal(signal.SIGINT, self._previous or signal.default_int_handler)
        return False

    def _handle(self, signum, frame):
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        try:
            print("\nStopping before the next download (the current one finishes first). "
                  "Press Ctrl+C again to stop at once.")
        except Exception:
            pass


def _print_unavailable_on_spotify(items):
    """v0.16.47 (Checklist item 40): the playlist's items Spotify gives no
    details for (v0.16.39), which the counts above leave out."""
    if not items:
        return
    print(f"Unavailable on Spotify ({len(items)}) -- listed in the playlist, but Spotify gives no song "
          "details, so they can't be downloaded (they keep their Track # and aren't counted as missing):")
    for u in items:
        name = f" {u.name}" if u.name else ""
        print(f"  - #{u.list_position}{name}: {u.reason_text}")


def _run_sync(config, entry, folder, max_new_downloads=None):
    """Runs one playlist's sync with progress printed, and reports any
    failures with their specific reasons afterward.

    v0.16.47: Ctrl+C stops the sync before its next download (see
    _StopOnCtrlC). Returns False when the user asked to stop, so a Sync of
    several playlists doesn't go on to the next one; True otherwise."""
    print()
    name = entry["folder_name"]
    with _StopOnCtrlC() as stopper:
        def on_progress(msg):
            if stopper.requested and _DOWNLOAD_START_RE.match(msg):
                raise _SyncStopped()
            print(msg)

        try:
            result = library_sync.sync_playlist(
                entry, folder, max_new_downloads=max_new_downloads, on_progress=on_progress
            )
        except _SyncStopped:
            logger.log_event(f"CLI: sync of '{name}' stopped by the user (Ctrl+C).")
            print(f"\nSync of '{name}' stopped. Songs downloaded so far are kept; removing songs no "
                  "longer in the playlist and the Sync Log entry were skipped (the next sync does them).")
            return False
        except RuntimeError as e:
            print(f"\nSync failed: {e}")
            return not stopper.requested

    _print_unavailable_on_spotify(result.unavailable_on_spotify)

    if result.failed:
        print(f"\nTracks that need attention in '{name}':")
        for track_name, reason in result.failed:
            print(f"  - {track_name}: {reason}")
        print("\"No match\" and \"Video unavailable\" need an exception link (Edit -> Exceptions). "
              "\"Age-restricted\" needs YouTube sign-in (Settings -> Sign-in) or an exception link to an "
              "upload that isn't age-restricted. The others are usually temporary; the next sync tries "
              "them again.")

    if stopper.requested:
        print(f"\n'{name}' had no download left to stop before, so its sync finished.")
        return False
    return True


def _with_new_releases(config, entries):
    """v0.16.50 (Checklist item 41c): check the watched artists whose
    albums are among `entries` for new releases, add those (as the GUI
    does), and return entries + the new albums, so this Sync / Scan covers
    them too."""
    new = []
    for artist_id in artist_watch.watched_artist_ids(config, entries):
        artist = config_manager.find_artist(config, artist_id) or {}
        print(f"\nChecking {artist.get('name', 'an artist')} for new releases...")
        added, error = artist_watch.check_artist(config, artist_id)
        if error:
            print(f"Couldn't check it: {error}")
        elif added:
            print(f"{len(added)} new release(s) added:")
            for e in added:
                print(f"  + {e['folder_name']}")
        else:
            print("No new releases.")
        new.extend(added)
    return list(entries) + new


def handle_sync(config):
    playlists = _print_playlists(config)
    if not playlists:
        return

    if not _require_library_path(config):
        return

    selection = input(
        "\nWhich playlists to sync? (e.g. 1, 4, 7-9) or 'done' to cancel: "
    ).strip()
    if not selection or selection.lower() == "done":
        return

    indices = _parse_selection(selection, len(playlists))
    if not indices:
        print("No valid playlists selected.")
        return

    cap_input = input(
        "Limit how many NEW tracks to download per playlist this run? "
        "(press Enter for no limit): "
    ).strip()
    max_new_downloads = int(cap_input) if cap_input.isdigit() else None

    entries = _with_new_releases(config, [playlists[i - 1] for i in indices])
    for n, entry in enumerate(entries, start=1):
        print(f"\n=== Syncing '{entry['folder_name']}' ===")
        try:
            folder = config_manager.get_playlist_folder(config, entry)
        except ValueError as e:
            print(f"Skipped: {e}")
            continue
        if not _run_sync(config, entry, folder, max_new_downloads=max_new_downloads):
            left = len(entries) - n
            if left:
                print(f"The other {left} playlist(s) weren't synced.")
            return


# ---------------------------------------------------------------------------
# Scan (read-only dry-run sync) -- Checklist item 2
# ---------------------------------------------------------------------------

def _print_scan_result(entry, result):
    """Prints a clean, read-only summary of what a real Sync would do
    to this playlist, per Checklist item 2: which tracks are missing
    locally, which local tracks are no longer live, which would be
    renumbered, and how many are already fully in sync."""
    print(f"\n--- '{entry['folder_name']}' ---")

    if result.missing:
        print(f"Missing locally ({len(result.missing)}) -- a Sync would download these:")
        for t in result.missing:
            # v0.16.27 (Checklist item 28): why an earlier sync failed it, if it did.
            record = result.known_failures.get(track_failures.track_key(t))
            note = f"  [last sync: {track_failures.summary(record)}]" if record else ""
            print(f"  - {t.name} - {t.primary_artist}{note}")

    if result.no_longer_live:
        print(f"No longer in the live playlist ({len(result.no_longer_live)}) -- a Sync would remove these files:")
        for name in result.no_longer_live:
            print(f"  - {name}")

    if result.needs_renumber:
        print(f"Would be renumbered ({len(result.needs_renumber)}):")
        for name in result.needs_renumber:
            print(f"  - {name}")

    print(f"Already in sync: {result.unchanged_count}")
    _print_unavailable_on_spotify(result.unavailable_on_spotify)

    if not result.missing and not result.no_longer_live and not result.needs_renumber:
        print("Fully in sync -- a real Sync would have nothing to do here.")


def handle_scan(config):
    """
    Dry-run version of Sync (Checklist item 2). Runs the exact same
    live-vs-local diff logic as a real Sync but never downloads,
    deletes, or tags anything -- purely an inspection tool, and safe
    to run at any time (e.g. before committing to a real Sync on a
    playlist you haven't touched in a while).
    """
    playlists = _print_playlists(config)
    if not playlists:
        return

    selection = input(
        "\nWhich playlists to scan? (e.g. 1, 4, 7-9) or 'done' to cancel: "
    ).strip()
    if not selection or selection.lower() == "done":
        return

    indices = _parse_selection(selection, len(playlists))
    if not indices:
        print("No valid playlists selected.")
        return

    for entry in _with_new_releases(config, [playlists[i - 1] for i in indices]):
        print(f"\n=== Scanning '{entry['folder_name']}' ===")
        try:
            folder = config_manager.get_playlist_folder(config, entry)
        except ValueError as e:
            print(f"Skipped: {e}")
            continue

        try:
            result = library_sync.scan_playlist(entry, folder, on_progress=print)
        except RuntimeError as e:
            print(f"\nScan failed: {e}")
            continue

        _print_scan_result(entry, result)


# ---------------------------------------------------------------------------
# Check Integrity (read-only file-health scan) -- Checklist item 3
# ---------------------------------------------------------------------------

def _print_integrity_result(entry, result):
    """Prints a clean, read-only summary of a playlist folder's
    file-level health, per Checklist item 3: leftover files from an
    interrupted download/tagging/renumber, unreadable (possibly
    corrupted) files, and untagged files that Sync/Scan can't see."""
    print(f"\n--- '{entry['folder_name']}' ---")

    if result.orphaned_temp_files:
        print(
            f"Leftover files from an interrupted download/tagging step "
            f"({len(result.orphaned_temp_files)}):"
        )
        for path in result.orphaned_temp_files:
            print(f"  - {path.name}")

    if result.unreadable_files:
        print(f"Files whose tags couldn't be read at all -- possibly corrupted ({len(result.unreadable_files)}):")
        for name in result.unreadable_files:
            print(f"  - {name}")

    if result.untagged_files:
        print(
            f"Files with no identity (Track ID) tag -- invisible to Sync/Scan "
            f"({len(result.untagged_files)}):"
        )
        for name in result.untagged_files:
            print(f"  - {name}")

    if result.clean:
        print("No issues found.")


def handle_check_integrity(config):
    """
    Checklist item 3: read-only inspection of one or more playlist
    folders for signs of a prior crash or interruption, and for files a
    normal Sync/Scan silently can't identify (see
    library_sync.check_playlist_integrity()). Never deletes or modifies
    anything on its own -- if leftover temp files are found, a separate,
    explicit confirmation step offers to clean them up afterward.
    Unreadable/untagged files are reported but never auto-fixed, since
    those need Michael's own judgment call.
    """
    playlists = _print_playlists(config)
    if not playlists:
        return

    selection = input(
        "\nWhich playlists to check? (e.g. 1, 4, 7-9) or 'done' to cancel: "
    ).strip()
    if not selection or selection.lower() == "done":
        return

    indices = _parse_selection(selection, len(playlists))
    if not indices:
        print("No valid playlists selected.")
        return

    to_clean = []  # (entry, folder) pairs that had orphaned temp files
    for i in indices:
        entry = playlists[i - 1]
        try:
            folder = config_manager.get_playlist_folder(config, entry)
        except ValueError as e:
            print(f"\nSkipped '{entry['folder_name']}': {e}")
            continue

        result = library_sync.check_playlist_integrity(folder)
        _print_integrity_result(entry, result)
        if result.orphaned_temp_files:
            to_clean.append((entry, folder))

    if not to_clean:
        return

    confirm = input(
        "\nDelete the leftover interrupted-download/tagging files listed above? (y/n): "
    ).strip().lower()
    if confirm != "y":
        print("Left in place -- nothing deleted.")
        return

    for entry, folder in to_clean:
        removed = library_sync.clean_orphaned_temp_files(folder)
        print(f"'{entry['folder_name']}': removed {removed} file(s).")


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------

def handle_remove(config):
    while True:
        entry = _select_tracked_playlist(config, prompt="Which playlist to remove? (or 'done'): ")
        if not entry:
            return

        confirm = input(
            f"Type the playlist's name exactly ('{entry['folder_name']}') to confirm removal: "
        ).strip()
        if confirm.lower() != entry["folder_name"].lower():
            print("Names didn't match -- nothing removed.")
            continue

        key = ui_settings.playlist_key(entry)
        try:
            config_manager.remove_playlist(config, entry["folder_name"])
        except ValueError as e:
            print(f"\nCouldn't remove that playlist: {e}")
            continue
        # v0.16.47 (Checklist item 40): drop its cached numbers, as the GUI's Remove does.
        ui_settings.forget_playlist(key)
        print(f"\nRemoved '{entry['folder_name']}' from active syncing. Its files were NOT deleted.")


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

def handle_edit(config):
    while True:
        entry = _select_tracked_playlist(config, prompt="Which playlist to edit? (or 'done'): ")
        if not entry:
            return
        _edit_one_playlist(config, entry)


def _edit_one_playlist(config, entry):
    while True:
        print(f"\nEditing '{entry['folder_name']}':")
        print("\tName | URL | Reorder | Exceptions | Logs")
        print("\nDone")
        choice = input("> ").strip()
        action = _match_keyword(choice, EDIT_MENU_ACTIONS)

        if action == "name":
            new_name = input("New name: ").strip()
            try:
                entry = config_manager.rename_playlist(config, entry["folder_name"], new_name)
                print(f"\nRenamed to '{entry['folder_name']}'.")
            except ValueError as e:
                print(f"\nCouldn't rename: {e}")

        elif action == "url":
            # v0.16.48 (Checklist item 41a): an album's link changes to another album.
            album = config_manager.is_album(entry)
            platform_label = config_manager.platform_name(config_manager.get_playlist_platform(entry))
            what = "album" if album else "playlist"
            print(
                f"\nWARNING: changing the {platform_label} link means every file currently in this "
                f"{what}'s folder will look 'no longer live' on the next sync, and will be "
                f"removed before the new {what}'s tracks are downloaded."
            )
            confirm = input("Type 'yes' to continue: ").strip().lower()
            if confirm != "yes":
                print("Cancelled -- nothing changed.")
                continue
            new_url = input(f"New {platform_label} album link: " if album
                            else f"New {platform_label} playlist link or ID: ").strip()
            old_key = ui_settings.playlist_key(entry)
            try:
                entry = config_manager.update_playlist_source_id(config, entry["folder_name"], new_url)
            except ValueError as e:
                print(f"\nCouldn't update: {e}")
                continue
            # v0.16.47 (Checklist item 40): as the GUI's Edit page does, the
            # playlist's custom picture follows it to its new link, and the
            # cached numbers of the old playlist are dropped.
            ui_settings.playlist_source_changed(old_key, ui_settings.playlist_key(entry))
            print(f"\n'{entry['folder_name']}' now tracks a different {platform_label} {what}.")

        elif action == "reorder":
            playlists = config_manager.list_playlists(config)
            current_index = next(
                (i for i, e in enumerate(playlists, start=1) if e["folder_name"] == entry["folder_name"]), None
            )
            print(f"\n'{entry['folder_name']}' is currently at position {current_index} of {len(playlists)}.")
            # v0.16.49: playlists and albums are separate lists; it moves within its own.
            first, last = config_manager.section_range(config, entry)
            new_pos = input(f"New position ({first}-{last}): ").strip()
            if not new_pos.isdigit():
                print("Not a valid position.")
                continue
            try:
                entry = config_manager.reorder_playlist(config, entry["folder_name"], int(new_pos))
                print(f"\nMoved '{entry['folder_name']}' to position {new_pos}.")
            except ValueError as e:
                print(f"\nCouldn't reorder: {e}")

        elif action == "exceptions":
            _edit_exceptions(config, entry)

        elif action == "logs":
            _view_sync_log(config, entry)

        elif action == "done":
            return
        else:
            print("Not a valid option.")


def _view_sync_log(config, entry):
    """Print this playlist's Sync Log.txt (the same file the GUI's
    'View Sync Logs' button opens)."""
    try:
        folder = config_manager.get_playlist_folder(config, entry)
    except ValueError as e:
        print(f"\nCan't locate the folder: {e}")
        return
    log_path = Path(folder) / library_sync.DOWNLOAD_LOG_FILENAME
    if not log_path.exists():
        print(f"\nNo Sync Log yet for '{entry['folder_name']}' (one is written after a sync that downloads songs).")
        return
    print(f"\n--- {log_path} ---")
    try:
        print(log_path.read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        print(f"Couldn't read the log: {e}")


def _song_names_for_exceptions(config, entry):
    """v0.16.47 (Checklist item 40): {exception key: "Artist - Title"} for
    the Exceptions list, read with no network: from the last scan's
    not-downloaded songs the GUI caches, then from the playlist folder's
    own files (which win). Exceptions are keyed by Spotify track ID, or
    for a YouTube playlist by the bare video ID (the file's Comment holds
    'yt:<videoId>')."""
    def label(title, artist):
        return f"{artist} - {title}" if artist else (title or "")

    names = {}
    try:
        for record in ui_settings.get_cache(ui_settings.playlist_key(entry)).get("missing") or []:
            if record.get("key") and record.get("title"):
                names[record["key"]] = label(record["title"], record.get("artist"))
    except Exception as e:
        logger.log_error(f"CLI: couldn't read cached song names for '{entry['folder_name']}': {e}")

    try:
        folder = config_manager.get_playlist_folder(config, entry)
    except ValueError:
        return names
    youtube = config_manager.uses_video_ids(entry) or config_manager.is_soundcloud(entry)  # YTM, SC too
    prefix = (metadata_tagger.SOUNDCLOUD_IDENTITY_PREFIX if config_manager.is_soundcloud(entry)
              else metadata_tagger.YOUTUBE_IDENTITY_PREFIX)
    for path in metadata_tagger.audio_files(folder):
        tags = metadata_tagger.read_tags(path)
        if tags is None or not tags.identity:
            continue
        key = tags.identity[len(prefix):] if youtube and tags.identity.startswith(prefix) else tags.identity
        names[key] = label(tags.title or path.stem, tags.artist)
    return names


def _print_exceptions(config, entry):
    """Always show the current exceptions for this playlist, the same
    way tracked playlists are always shown on the main menu -- no
    separate 'view' option needed. Since v0.16.47 each shows its song's
    name (the ID only when the song isn't known locally)."""
    saved = exceptions_manager.list_exceptions(config_manager.get_playlist_source_id(entry))
    if not saved:
        print(f"\nNo exceptions saved for '{entry['folder_name']}'.")
        return saved

    names = _song_names_for_exceptions(config, entry)
    print(f"\nExceptions for '{entry['folder_name']}' ({len(saved)}):")
    for i, (track_id, video_id) in enumerate(saved.items(), start=1):
        song = names.get(track_id) or f"track {track_id}"
        print(f"  {i}) {song}  ->  https://www.youtube.com/watch?v={video_id}")
    return saved


def _edit_exceptions(config, entry):
    while True:
        saved = _print_exceptions(config, entry)
        print("\nFix | Remove")
        print("\nDone")
        choice = input("> ").strip()
        action = _match_keyword(choice, EXCEPTIONS_MENU_ACTIONS)

        if action == "fix":
            _handle_fix_track_for(config, entry)
        elif action == "remove":
            _handle_remove_exception_for(entry, saved)
        elif action == "done":
            return
        else:
            print("Not a valid option.")


def _handle_fix_track_for(config, entry):
    if not _require_library_path(config) and not entry.get("custom_path"):
        return

    print("\nFetching the live playlist to pick a track (this can take a minute)...")
    unavailable = []  # Spotify items with no details: they still count toward Track #'s total
    if config_manager.is_soundcloud(entry):  # v0.16.52 (Checklist item 42b)
        tracks = acquisition_engine.get_soundcloud_tracks(entry)
    elif config_manager.uses_video_ids(entry):  # YouTube, and YT Music since v0.16.51
        # Only the video list is needed to pick a track here, not each
        # video's full tag data -- skip the expensive per-video fetch.
        # (A YT Music listing already has each song's title / artist / album.)
        tracks = acquisition_engine.get_video_tracks(entry, fetch_details=False)
    else:
        # Spotify, or Apple Music since v0.16.53 (albums too, v0.16.48)
        tracks = acquisition_engine.get_catalog_tracks(entry, unavailable=unavailable)
    track = _select_track(tracks)
    if not track:
        return

    youtube_link = input("Correct YouTube link or video ID: ").strip()
    folder = config_manager.get_playlist_folder(config, entry)

    print("\nFixing -- this may take a moment...")
    try:
        result = library_sync.fix_track(
            track, entry, folder, youtube_link,
            total_tracks=library_sync.position_total(tracks, unavailable),
        )
    except ValueError as e:
        print(f"\nCouldn't save that exception: {e}")
        return

    if result.file_path:
        print(f"\nFixed. Re-downloaded and tagged: {result.file_path} âœ…")
    else:
        print(f"\nThe exception was saved, but the download itself failed: {result.failure_reason}")


def _handle_remove_exception_for(entry, saved):
    if not saved:
        return

    track_ids = list(saved.keys())
    choice = input("Which one to remove? (or 'done'): ").strip()
    if not choice or choice.lower() == "done":
        return
    if not choice.isdigit() or not (1 <= int(choice) <= len(track_ids)):
        print("Not a valid choice.")
        return

    track_id = track_ids[int(choice) - 1]
    exceptions_manager.remove_exception(config_manager.get_playlist_source_id(entry), track_id)
    print("\nRemoved. This does not undo or delete the file itself -- use "
          "'Fix' if you also want to re-fix it.")


# ---------------------------------------------------------------------------
# Library folder path -- Checklist item 4: move-or-relocate UX
# ---------------------------------------------------------------------------

# Typing any of these at the move-or-repoint prompt backs out of
# changing the library folder entirely, with nothing touched.
LIBRARY_PATH_CANCEL_WORDS = {"cancel", "done", "back"}


def _scan_after_library_change(config):
    """
    Checklist item 4: right after a Library-path change (moved or just
    repointed), runs the exact same live-vs-local diff Scan uses
    (Checklist item 2 -- library_sync.scan_playlist()) against every
    tracked playlist that doesn't have a custom_path (a custom_path
    playlist's location isn't touched by a Library-path change at
    all). This is a real check against the live Spotify playlist, not
    just a filesystem presence check -- it confirms whether each
    playlist is still actually fully in sync at the (new) current
    location: anything missing, anything no longer live, and anything
    that would need renumbering are all reported exactly as they would
    be from the main Scan menu option.
    """
    playlists = [e for e in config_manager.list_playlists(config) if not e.get("custom_path")]
    if not playlists:
        return

    print("\nChecking whether your tracked playlists are still fully in sync at this location...")
    for entry in playlists:
        try:
            folder = config_manager.get_playlist_folder(config, entry)
        except ValueError as e:
            print(f"\nSkipped '{entry['folder_name']}': {e}")
            continue

        try:
            result = library_sync.scan_playlist(entry, folder, on_progress=print)
        except RuntimeError as e:
            print(f"\nCouldn't check '{entry['folder_name']}': {e}")
            continue

        _print_scan_result(entry, result)


def handle_set_library_path(config):
    current = config_manager.get_library_path(config)
    print(f"\nCurrent library folder: {current or '(not set yet)'}")

    move_files = False
    if current:
        move_choice = input(
            "\nWhen you pick the new folder, should your existing playlist files be MOVED "
            "there too, or should the app just be repointed there (existing files stay "
            "exactly where they are)? Type 'move' or 'repoint' (or 'cancel'/'done'/'back' "
            "to leave things as they are): "
        ).strip().lower()

        if move_choice in LIBRARY_PATH_CANCEL_WORDS:
            print("Cancelled -- nothing changed.")
            return

        move_files = move_choice == "move"
        if move_files:
            print("\nMoving all files: your existing playlist folders will be moved to wherever you pick next.")
        else:
            print("\nRepointing: the app will look in the new folder going forward, but nothing will be moved.")

    print("\nOpening a folder picker window...")
    new_path = _pick_folder_dialog()
    if not new_path:
        manual = input(
            "No folder was selected -- type a path manually, or press Enter to cancel: "
        ).strip()
        if not manual:
            print("Nothing changed.")
            return
        new_path = manual

    if move_files:
        print("\nMoving all files to the new location -- this can take a while for a large library...")
    else:
        print("\nRepointing the library folder...")

    try:
        resolved = config_manager.change_library_path(config, new_path, move_files=move_files, on_progress=print)
    except ValueError as e:
        print(f"\nCouldn't set that path: {e}")
        return

    if move_files and config_manager.get_library_path(config) != resolved:
        print(
            "\nOne or more playlist folders failed to move -- see logs/error_log.txt for the "
            "specific reason(s). The library folder path has deliberately NOT been changed "
            "yet, so nothing is left half-moved: your files are still safely at the old "
            "location. Run 'Change Music Folder Path' again to retry."
        )
        return

    print(f"\nLibrary folder is now: {resolved}")
    if not move_files:
        print("(Existing files were not moved -- only new syncs will use this location.)")

    _scan_after_library_change(config)


# ---------------------------------------------------------------------------
# Update Project Libraries
# ---------------------------------------------------------------------------

def handle_update_libraries(dep_statuses):
    outdated = [s for s in dep_statuses if s.outdated]
    if not outdated:
        print("\nEverything is up to date.")
        return dep_statuses

    print(f"\n{len(outdated)} package(s) have updates available:")
    for s in outdated:
        print(f"  - {s.name}: {s.installed_version} -> {s.latest_version}")

    confirm = input("Update them now? (y/n): ").strip().lower()
    if confirm != "y":
        print("Cancelled -- nothing updated.")
        return dep_statuses

    print()
    results = dependency_checker.update_outdated(dep_statuses)
    for line in results:
        print(line)
    print("\nRestart Mud Music Manager for these updates to actually take effect.")

    return dependency_checker.check_all()


# ---------------------------------------------------------------------------
# Settings (v0.16.0) -- the same preferences as the GUI's Settings page
# ---------------------------------------------------------------------------

def _download_quality_line():
    """One-line state of Settings -> Download quality, as the GUI's status line words it."""
    changed = download_settings.changed_keys()
    if not changed:
        return "using the default settings"
    return "changed: " + ", ".join(download_settings.NAMES[key] for key in changed)


def _signin_line():
    s = youtube_cookies.get_setting()
    if not youtube_cookies.is_on(s):
        return "Off" if s["mode"] != youtube_cookies.MODE_FILE else "Off (no cookies.txt chosen)"
    return f"on, with {youtube_cookies.describe(s)}"


def handle_settings(config):
    while True:
        print("\nSettings:")
        print(f"  Theme:             {ui_settings.get('theme')}   (used by the graphical app)")
        cmd_label = "separate app window" if ui_settings.get("cmd_mode") == ui_settings.CMD_WINDOW else "real OS terminal"
        print(f"  CMD button:        {cmd_label}   (what the GUI's CMD button opens)")
        print(f"  Prefer explicit:   {'On' if ui_settings.get('prefer_explicit') else 'Off'}   "
              f"(pick a song's explicit version over its clean one)")
        print(f"  Download quality:  {_download_quality_line()}")
        print(f"  YouTube sign-in:   {_signin_line()}")
        print("  Your data:         Export / Import a backup of your playlists, exceptions and settings")
        print("\n\tTheme | CMD | Explicit | Download Quality | Sign-in | Libraries")
        print("\tExport | Import")
        print("\nDone")
        choice = input("> ").strip()
        action = _match_keyword(choice, SETTINGS_MENU_ACTIONS)

        if action == "theme":
            pick = input("Theme (dark/light): ").strip().lower()
            if pick in (ui_settings.THEME_DARK, ui_settings.THEME_LIGHT):
                ui_settings.set_value("theme", pick)
                print(f"Theme set to {pick}.")
            else:
                print("Not a valid theme.")
        elif action == "cmd":
            pick = input("CMD button opens (window/terminal): ").strip().lower()
            if pick in (ui_settings.CMD_WINDOW, ui_settings.CMD_TERMINAL):
                ui_settings.set_value("cmd_mode", pick)
                print(f"CMD button will open: {pick}.")
            else:
                print("Not a valid option.")
        elif action == "explicit":
            # v0.17.7 (Checklist item 58): Settings -> Prefer explicit versions
            pick = input("Prefer explicit versions (on/off): ").strip().lower()
            if pick in ("on", "off"):
                ui_settings.set_value("prefer_explicit", pick == "on")
                print(f"Prefer explicit versions: {pick.capitalize()}. Applies to songs downloaded from now on.")
            else:
                print("Not a valid option.")
        elif action == "download":
            _edit_download_settings()
        elif action == "signin":
            _edit_youtube_signin()
        elif action == "libraries":
            _print_libraries()
        elif action == "export":
            _export_user_data()
        elif action == "import":
            _import_user_data(config)
        elif action == "done":
            return
        else:
            print("Not a valid option.")


# v0.17.9 (Checklist item 59): Settings -> Export / Import, the same backup
# file as the GUI's Settings -> Your data (user_data.py, design doc §3.10).

def _documents_dir():
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return docs if os.path.isdir(docs) else os.path.expanduser("~")


def _ask_path(prompt):
    """A typed path, with quotes from a pasted / dragged-in path removed."""
    return input(prompt).strip().strip('"').strip("'").strip()


def _export_user_data():
    default = os.path.join(_documents_dir(), user_data.default_filename())
    print("\nExport your data -- one backup file with your playlists, albums, artists, exceptions, settings "
          "and playlist pictures. Your music files aren't in it.")
    path = _ask_path(f"Save to (Enter for {default}): ") or default
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(path):
        path = os.path.join(path, user_data.default_filename())
    if not path.lower().endswith(user_data.FILE_EXTENSION):
        path += user_data.FILE_EXTENSION
    if os.path.exists(path):
        if input(f"{path} already exists. Replace it? (y/n): ").strip().lower() != "y":
            print("Cancelled -- nothing exported.")
            return
    try:
        counts = user_data.export_data(path, VERSION)
    except user_data.BackupError as e:
        print(f"Export failed: {e}")
        return
    print(f"\nSaved to: {path}")
    print(f"  {counts.describe()}, and your settings.")
    print("To use it on another computer, install Mud Music Manager there, then choose Settings -> Import "
          "(here, or Settings -> Your data -> Import... in the app) and give it this file. Your music files "
          "aren't in the backup: Sync downloads them again, or copy your Music folder across too.")


def _import_user_data(config):
    if os.environ.get(debug_console.CLI_FROM_GUI_ENV):
        # The app keeps its settings in memory and saves them often (e.g.
        # after every scan), so it would write them back over the import.
        print("\nImport isn't available here while the graphical app is open: the app would write its own "
              "settings back over the imported ones. Import from the app instead (Settings -> Your data -> "
              "Import...), or close the app and run this menu on its own (python main.py --cli).")
        return
    print("\nImport your data -- replaces the playlists, albums, artists, exceptions and settings here with a "
          "backup's. Your music files are not touched. Close the graphical app first if it is open.")
    path = _ask_path("Backup file (Enter to cancel): ")
    if not path:
        print("Cancelled -- nothing imported.")
        return
    try:
        backup = user_data.read_backup(os.path.abspath(os.path.expanduser(path)))
    except user_data.BackupError as e:
        print(f"Can't import that file: {e}")
        return

    print(f"\nBackup made {backup.created_text()} by Mud Music Manager v{backup.app_version}:")
    print(f"  {backup.counts.describe()}, and your settings.")
    library_path = user_data.library_path_after_import(backup)
    if library_path and library_path == backup.config.get("library_path"):
        print(f"  Music folder: {library_path} (the backup's, found on this computer)")
    elif library_path:
        print(f"  Music folder: {library_path} (this computer's, kept)")
    else:
        print("  Music folder: none yet -- set it afterwards with Change Music Folder Path.")
    print("\nThis REPLACES the playlists, albums, artists, exceptions and settings on this computer with the "
          "backup's. Your current data is saved first, so you can go back by importing that copy.")
    if input("Replace them now? (y/n): ").strip().lower() != "y":
        print("Cancelled -- nothing imported.")
        return

    try:
        report = user_data.import_backup(backup, VERSION)
    except user_data.BackupError as e:
        print(f"\nImport failed: {e}")
        _reload_config_in_place(config)  # show whatever is on disk now
        return
    _reload_config_in_place(config)

    print(f"\nImported {report.counts.describe()}, and your settings.")
    if report.library_path:
        print(f"Music folder: {report.library_path}")
    else:
        print("No Music folder is set yet: choose one with Change Music Folder Path, then Sync to download "
              "your songs.")
    if report.missing_custom_folders:
        print(f"Linked to a folder that isn't on this computer: {', '.join(report.missing_custom_folders)}. "
              f"A Sync puts their songs at that same path if it can; to keep them in the Music folder instead, "
              f"remove and add them again.")
    if report.missing_cookie_file:
        print(f"YouTube sign-in uses a cookies.txt file that isn't on this computer "
              f"({report.missing_cookie_file}). Choose it again under Settings -> Sign-in.")
    print(f"Your data from before the import is saved in:\n  {report.safety_copy}")


def _reload_config_in_place(config):
    """Re-read playlists.json into the dict the main loop holds (as the
    GUI's AppContext.reload_config() does)."""
    try:
        fresh = config_manager.load_config()
    except SystemExit as e:
        print(f"Couldn't reload the playlists: {e}")
        return
    config.clear()
    config.update(fresh)


# v0.16.47 (Checklist item 40): the same two settings as the GUI's Settings
# page, so they can be changed when the graphical app can't start. Both are
# stored in ui_settings.json and read by every sync (GUI or terminal).

def _edit_download_settings():
    """Settings -> Download quality (download_settings.py, Â§3.1 / Â§3.10)."""
    keys = list(download_settings.OPTIONS)
    while True:
        s = download_settings.get_setting()
        print("\nDownload quality -- used by every sync, here and in the graphical app. A change applies "
              "to new downloads only; songs already downloaded are never changed.")
        for i, key in enumerate(keys, start=1):
            note = "" if s[key] == download_settings.DEFAULTS[key] else "   (changed)"
            if key == "bitrate" and not download_settings.bitrate_applies(s):
                note += f"   (not used for .{s['file_format']}: it is lossless)"
            elif key == "source" and s["file_format"] != "m4a":
                note += "   (only matters for .m4a)"
            print(f"  {i}) {download_settings.NAMES[key]}: {download_settings.label(key, s[key])}{note}")
        print(f"\n{'Using the default settings.' if download_settings.is_default(s) else 'Some settings differ from the defaults.'}")
        print("\nType a number to change that setting" + (" | Reset" if not download_settings.is_default(s) else ""))
        print("\nDone")
        choice = input("> ").strip()

        if choice.isdigit() and 1 <= int(choice) <= len(keys):
            _pick_download_setting(keys[int(choice) - 1])
            continue
        action = _match_keyword(choice, DOWNLOAD_MENU_ACTIONS)
        if action == "reset":
            if download_settings.is_default():
                print("Already using the default settings.")
            else:
                download_settings.reset()
                print("Download quality is back to the default settings.")
        elif action == "done":
            return
        else:
            print("Not a valid option.")


def _pick_download_setting(key):
    choices = download_settings.OPTIONS[key]
    current = download_settings.get_setting()[key]
    print(f"\n{download_settings.NAMES[key]}:")
    for i, (value, label, desc) in enumerate(choices, start=1):
        marks = [m for m, on in (("default", i == 1), ("current", value == current)) if on]
        mark = f"  ({', '.join(marks)})" if marks else ""
        print(f"  {i}) {label}{mark}")
        if desc:
            print(f"       {desc}")
    pick = input("Number (or press Enter to keep it as it is): ").strip()
    if not pick:
        return
    if not pick.isdigit() or not (1 <= int(pick) <= len(choices)):
        print("Not a valid choice -- nothing changed.")
        return
    value, label, _ = choices[int(pick) - 1]
    download_settings.set_setting(**{key: value})
    print(f"{download_settings.NAMES[key]} set to {label}.")


def _edit_youtube_signin():
    """Settings -> YouTube sign-in (youtube_cookies.py, Â§3.6)."""
    while True:
        s = youtube_cookies.get_setting()
        print("\nYouTube sign-in -- only used as a retry: a download YouTube refuses as age-restricted, or "
              "with its \"not a bot\" check, is tried once more signed in. Every other download stays anonymous.")
        print(f"  Now: {_signin_line()}")
        if s["mode"] == youtube_cookies.MODE_BROWSER:
            note = youtube_cookies.chromium_note(s["browser"])
            if note:
                print(f"  Note: {note}")
        elif s["mode"] == youtube_cookies.MODE_FILE and s["file"]:
            print(f"  File: {s['file']}")
        print("\n\tOff | Browser | Cookies.txt | Test")
        print("\nDone")
        action = _match_keyword(input("> ").strip(), SIGNIN_MENU_ACTIONS)

        if action == "off":
            youtube_cookies.set_setting(mode=youtube_cookies.MODE_OFF)
            print("YouTube sign-in is off.")
        elif action == "browser":
            options = youtube_cookies.browsers()
            print("\nWhich browser's YouTube cookies? (its default profile is used)")
            for i, (name, label) in enumerate(options, start=1):
                mark = "  (current)" if name == s["browser"] and s["mode"] == youtube_cookies.MODE_BROWSER else ""
                print(f"  {i}) {label}{mark}")
            pick = input("Number (or press Enter to cancel): ").strip()
            if not pick:
                continue
            if not pick.isdigit() or not (1 <= int(pick) <= len(options)):
                print("Not a valid choice -- nothing changed.")
                continue
            name, label = options[int(pick) - 1]
            youtube_cookies.set_setting(mode=youtube_cookies.MODE_BROWSER, browser=name)
            print(f"YouTube sign-in will use {label}'s cookies. Type 'test' to check them.")
            note = youtube_cookies.chromium_note(name)
            if note:
                print(note)
        elif action == "file":
            print("\nA cookies.txt in Netscape format, exported with a \"Get cookies.txt\" browser extension "
                  "while signed in to youtube.com. It is only read, never changed.")
            path = input("Full path to the cookies.txt file (or press Enter to cancel): ").strip().strip('"')
            if not path:
                continue
            if not os.path.isfile(path):
                print("That file wasn't found -- nothing changed.")
                continue
            youtube_cookies.set_setting(mode=youtube_cookies.MODE_FILE, file=os.path.abspath(path))
            print("YouTube sign-in will use that cookies.txt. Type 'test' to check it.")
        elif action == "test":
            print("\nReading the cookies (this can take a few seconds)...")
            _ok, message = youtube_cookies.test()
            print(message)
        elif action == "done":
            return
        else:
            print("Not a valid option.")


def _print_libraries():
    print("\nProject libraries (from requirements.txt):")
    for info in ui_libraries.describe_all():
        if info.installed:
            print(f"  {info.name} {info.version}\n      {info.path}")
        else:
            print(f"  {info.name}: not installed")
    env = ui_libraries.python_environment()
    print(f"\nPython {env['version']}: {env['executable']}")
    print(f"Packages folder: {env['site_packages']}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_cli():
    note = os.environ.get(debug_console.CLI_NOTE_ENV)
    if note:
        print(note + "\n")

    config = config_manager.load_config()

    print("Checking for package updates...")
    dep_statuses = dependency_checker.check_all()

    while True:
        print("\n" + "=" * 50)
        print(f"  Mud Music Manager -- v{VERSION}")
        print(f"  {app_info.COPYRIGHT_ASCII}")  # v0.17.1 (Checklist item 52)
        print("=" * 50)
        print(f"Library folder: {config_manager.get_library_path(config) or '(not set)'}")
        _print_playlists(config)

        print("\nWhat would you like to do?")
        print("\n\tAdd | Edit | Remove")
        print("\tSync")
        print("\tScan")
        print("\tCheck Integrity")
        print()
        print("\tChange Music Folder Path")
        print("\tSettings")
        print("\tUpdate Project Libraries")
        print(f"\t   {_dependency_status_line(dep_statuses)}")
        print()
        print("Done")

        try:
            choice = input("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            # v0.16.47: Ctrl+C (or the input closing) at the main menu is "Done".
            print("\nGoodbye!")
            break
        action = _match_keyword(choice, MAIN_MENU_ACTIONS)

        try:
            if action == "add":
                handle_add(config)
            elif action == "sync":
                handle_sync(config)
            elif action == "scan":
                handle_scan(config)
            elif action == "check":
                handle_check_integrity(config)
            elif action == "remove":
                handle_remove(config)
            elif action == "edit":
                handle_edit(config)
            elif action == "change_path":
                handle_set_library_path(config)
            elif action == "update":
                dep_statuses = handle_update_libraries(dep_statuses)
            elif action == "settings":
                handle_settings(config)
            elif action == "done":
                print("\nGoodbye!")
                break
            else:
                print("\nNot a valid option.")
        except KeyboardInterrupt:
            # v0.16.47 (Checklist item 40): Ctrl+C anywhere in a sub-menu goes
            # back to the main menu instead of ending it with a traceback.
            # Reloading playlists.json also finishes anything the interrupt
            # cut short (a rename or library move self-heals on load, Â§3.8).
            logger.log_event("CLI: Ctrl+C -- back to the main menu.")
            print("\n\nStopped -- back to the main menu.")
            config = config_manager.load_config()


_crash_log_file = None  # kept open for the life of the process (faulthandler needs it)


def _install_crash_diagnostics():
    """Added in v0.16.2 after the GUI closed itself mid-sync with nothing in
    the error log. Makes any such exit leave a trace:
      - logs/crash_log.txt gets a Python traceback of every thread if the
        process dies from a native crash (faulthandler), plus Qt's own
        warnings/fatal messages;
      - uncaught Python exceptions (main thread or any worker thread) are
        written to logs/error_log.txt instead of vanishing.
    Purely diagnostic; never raises."""
    global _crash_log_file
    try:
        import faulthandler
        import threading
        import traceback

        os.makedirs(logger.LOG_DIR, exist_ok=True)
        _crash_log_file = open(os.path.join(logger.LOG_DIR, "crash_log.txt"), "a", encoding="utf-8")
        _crash_log_file.write(f"\n--- session start {datetime.now():%Y-%m-%d %H:%M:%S} (v{VERSION}) ---\n")
        _crash_log_file.flush()
        faulthandler.enable(file=_crash_log_file, all_threads=True)

        def _log_exc(kind, exc_type, exc, tb):
            text = "".join(traceback.format_exception(exc_type, exc, tb)).strip()
            logger.log_error(f"Uncaught exception ({kind}):\n{text}")

        def _sys_hook(exc_type, exc, tb):
            _log_exc("main thread", exc_type, exc, tb)
            sys.__excepthook__(exc_type, exc, tb)

        def _thread_hook(args):
            _log_exc(f"thread {getattr(args.thread, 'name', '?')}", args.exc_type, args.exc_value, args.exc_traceback)

        sys.excepthook = _sys_hook
        threading.excepthook = _thread_hook

        try:
            from PySide6.QtCore import qInstallMessageHandler

            def _qt_message(mode, context, message):
                try:
                    _crash_log_file.write(f"[Qt {datetime.now():%H:%M:%S}] {message}\n")
                    _crash_log_file.flush()
                except Exception:
                    pass
                debug_console.write_line(f"[Qt] {message}")

            qInstallMessageHandler(_qt_message)
        except Exception:
            pass
    except Exception:
        pass


def _terminal_fallback(note):
    """Run the classic terminal menu. If the app has no console of its own
    (it was relaunched under pythonw.exe, v0.16.3), open one first, since
    there would otherwise be nowhere to show or type anything."""
    if debug_console.has_console():
        run_cli()
        return
    err = debug_console.launch_cli_in_new_console(os.path.abspath(__file__), note)
    if err:
        debug_console.show_message_box("Mud Music Manager", f"{note}\n\n{err}")


def main():
    """Default: the graphical app. `--cli` (or a failure to start the
    GUI, e.g. PySide6 not installed) uses the classic terminal menu."""
    if "--cli" in sys.argv[1:]:
        if debug_console.cli_can_run_here():  # a console, or the in-app CMD window's pipes (v0.16.7)
            run_cli()
        else:
            # `--cli` started with no terminal attached (e.g. via pythonw.exe)
            err = debug_console.launch_cli_in_new_console(os.path.abspath(__file__))
            if err:
                debug_console.show_message_box("Mud Music Manager", err)
        return

    _install_crash_diagnostics()
    debug_console.install_capture()
    debug_console.write_line(f"Mud Music Manager v{VERSION} starting")

    try:
        import ui_app
    except ImportError as e:
        logger.log_error(f"GUI unavailable ({e}) -- falling back to the terminal menu.")
        print(f"The graphical app couldn't load ({e}).")
        print("Install its requirements with:  pip install -r requirements.txt")
        print("Starting the terminal menu instead...\n")
        _terminal_fallback(
            f"The graphical app couldn't load ({e}). Install its requirements with: "
            "pip install -r requirements.txt   -- starting the terminal menu instead.")
        return

    try:
        code = ui_app.run(VERSION)
    except SystemExit:
        raise
    except Exception as e:
        logger.log_error(f"GUI failed to start: {type(e).__name__}: {e}")
        print(f"The graphical app hit a problem and couldn't start ({type(e).__name__}: {e}).")
        print("Starting the terminal menu instead...\n")
        _terminal_fallback(
            f"The graphical app hit a problem and couldn't start ({type(e).__name__}: {e}) "
            "-- starting the terminal menu instead.")
        return
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        # With no console (pythonw.exe) a message passed to sys.exit("...") would vanish.
        if isinstance(e.code, str) and not debug_console.has_console():
            logger.log_error(f"Exit: {e.code}")
            debug_console.show_message_box("Mud Music Manager", e.code)
        raise
    except Exception as e:
        logger.log_error(f"Unhandled exception in main: {e}")
        raise
