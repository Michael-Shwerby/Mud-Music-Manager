"""
Mud Music Manager - Where the app's files live (v0.18.0)

The one place that decides where the program's own files are and where the
user's data is kept. Standard library only, and imports nothing from the
project, so every module (logger and debug_console included) can use it.

  APP_DIR       the folder the program runs from: main.py's folder, or the
                folder holding the .exe in a packaged build.
  RESOURCE_DIR  where the files shipped with the program are (assets/,
                LICENSE.txt, THIRD_PARTY_NOTICES.txt, requirements.txt).
                APP_DIR when running from source; PyInstaller's bundle
                folder (sys._MEIPASS) in a packaged build.
  TOOLS_DIR     APP_DIR/tools: FFmpeg and Deno shipped with a packaged
                build (v0.19.1), put first on PATH by add_tools_to_path().
  DATA_DIR      the user's data: playlists.json, exceptions.json,
                track_failures.json, ui_settings.json (each with its .bak),
                logs/, playlist_icons/ and backups/.

DATA_DIR is, in order:
  1. the folder named by the MMM_DATA_DIR environment variable, if set
     (for tests, or to keep data somewhere else on purpose);
  2. APP_DIR, when a file named portable.txt sits next to the program
     (the portable .zip build, which keeps everything in its own folder);
  3. otherwise the system's per-user app-data folder:
     %APPDATA%\\Mud Music Manager on Windows,
     ~/Library/Application Support/Mud Music Manager on macOS,
     $XDG_DATA_HOME (or ~/.local/share)/mud-music-manager on Linux.

Until v0.18.0 the data always lived in APP_DIR. Keeping it out of the
program folder means an installer or an update can replace the program
without touching the data, and the app works when installed somewhere it
can't write to.

Moving existing data (migrate_legacy_data(), run by prepare_data_dir()
before anything reads a data file): the first time a version from v0.18.0
on starts with data still in APP_DIR and none in DATA_DIR, the data is
COPIED there (the originals stay where they were, as a fallback, and a
note, "Your data has moved.txt", is left beside them). It uses the same
"record intent before touching disk, finish on the next launch" pattern as
the other crash safeguards (design doc §3.8): everything is copied into
DATA_DIR/.mmm_migration_tmp first; only then is .mmm_migration.json written
("moving") and each item moved into place with os.replace(); the state
becomes "done" last. A launch that finds "moving" finishes the moves. If
DATA_DIR already holds data, nothing is copied and the old files are left
alone. Custom playlist pictures that were inside APP_DIR/playlist_icons
have their saved paths pointed at the new folder.

If the data folder can't be created or the move fails, the app uses APP_DIR
for that session (exactly the old behaviour) and tries again next launch;
prepare_data_dir() returns a note saying so for the log.
"""

import json
import os
import shutil
import sys
import time
from typing import Optional

APP_FOLDER_NAME = "Mud Music Manager"  # app_info.APP_NAME (not imported: app_info imports this module)
DATA_DIR_ENV = "MMM_DATA_DIR"
PORTABLE_MARKER = "portable.txt"

FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR) if FROZEN else APP_DIR

# v0.19.1 (Checklist item 60c): a packaged build is two programs sharing one
# folder of libraries: the windowed app, and a console one for the terminal
# menu (`--cli`) and the Debug Window helper. FFmpeg and Deno ship in TOOLS_DIR.
GUI_EXE_NAME = "Mud Music Manager.exe"
CLI_EXE_NAME = "Mud Music Manager CLI.exe"
TOOLS_DIR = os.path.join(APP_DIR, "tools")


def cli_exe() -> str:
    """The packaged build's console program (only meaningful when FROZEN)."""
    return os.path.join(APP_DIR, CLI_EXE_NAME)


def add_tools_to_path() -> Optional[str]:
    """Put the bundled tools folder (ffmpeg.exe, ffprobe.exe, deno.exe) first
    on this process's PATH, so yt-dlp, downloader.ffmpeg_path() and every
    child process find them there before any copy installed on the computer.
    Does nothing when there is no tools folder (running from source with
    FFmpeg / Deno installed as before). Returns the folder added, or None."""
    if not os.path.isdir(TOOLS_DIR):
        return None
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if not any(_same_folder(p, TOOLS_DIR) for p in parts if p):
        os.environ["PATH"] = os.pathsep.join([TOOLS_DIR] + [p for p in parts if p])
    return TOOLS_DIR

# What a version before v0.18.0 kept in APP_DIR.
_MAIN_FILES = ("playlists.json", "exceptions.json", "track_failures.json", "ui_settings.json")
LEGACY_FILES = _MAIN_FILES + ("playlists.json.bak", "exceptions.json.bak", "track_failures.json.bak")
LEGACY_FOLDERS = ("logs", "playlist_icons", "backups")

MIGRATION_STATE = ".mmm_migration.json"
MIGRATION_STAGING = ".mmm_migration_tmp"
MOVED_NOTE = "Your data has moved.txt"


def is_portable() -> bool:
    """True when portable.txt sits next to the program (data stays in APP_DIR)."""
    return os.path.isfile(os.path.join(APP_DIR, PORTABLE_MARKER))


def _default_data_dir() -> str:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return os.path.abspath(override)
    if is_portable():
        return APP_DIR
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, APP_FOLDER_NAME)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_FOLDER_NAME)
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "mud-music-manager")


DATA_DIR = _default_data_dir()
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass  # prepare_data_dir() falls back to APP_DIR if it still can't be made


def data_path(*parts: str) -> str:
    """A path inside the user's data folder."""
    return os.path.join(DATA_DIR, *parts)


def resource_path(*parts: str) -> str:
    """A path to a file shipped with the program."""
    return os.path.join(RESOURCE_DIR, *parts)


def _same_folder(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def prepare_data_dir() -> Optional[str]:
    """Make sure DATA_DIR exists and holds the user's data, moving it from
    APP_DIR the first time (see the module docstring). Must run before any
    module reads or writes a data file; main.py calls it before importing
    them. Never raises. Returns a one-line note for the session log when
    something happened (data moved, or the fallback to APP_DIR), else None."""
    global DATA_DIR
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        return migrate_legacy_data()
    except Exception as e:
        if _same_folder(DATA_DIR, APP_DIR):
            return f"Couldn't prepare the data folder {DATA_DIR}: {e}"
        failed = DATA_DIR
        DATA_DIR = APP_DIR
        return (f"Couldn't use the data folder {failed} ({type(e).__name__}: {e}); "
                f"using {APP_DIR} for this session. It will be tried again next launch.")


# ---------------------------------------------------------------------------
# Moving data from APP_DIR (versions before v0.18.0)
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_state() -> Optional[dict]:
    try:
        with open(data_path(MIGRATION_STATE), "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else None
    except (OSError, ValueError):
        return None


def _write_state(state: dict) -> None:
    path = data_path(MIGRATION_STATE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _point_icons_at(settings_file: str, old_icons: str, new_icons: str) -> None:
    """Custom pictures inside the old playlist_icons folder: save their new
    path. Pictures stored anywhere else keep theirs."""
    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    icons = data.get("playlist_icons") if isinstance(data, dict) else None
    if not isinstance(icons, dict):
        return
    prefix = os.path.normcase(os.path.abspath(old_icons)) + os.sep
    changed = False
    for key, path in icons.items():
        if isinstance(path, str) and os.path.normcase(os.path.abspath(path)).startswith(prefix):
            icons[key] = os.path.join(new_icons, os.path.relpath(path, old_icons))
            changed = True
    if changed:
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def _move_into(src: str, dst: str) -> None:
    """Move a staged file or folder into the data folder. Folders are merged
    file by file. A file already there (e.g. a log written before an
    interrupted move was finished) is kept, and the moved one is put beside
    it as '<name> (from app folder)<ext>'."""
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            _move_into(os.path.join(src, name), os.path.join(dst, name))
        os.rmdir(src)
        return
    if os.path.exists(dst):
        stem, ext = os.path.splitext(dst)
        dst = f"{stem} (from app folder){ext}"
        if os.path.exists(dst):
            os.remove(dst)
    os.replace(src, dst)


def _write_moved_note(old_dir: str) -> None:
    text = (
        "Mud Music Manager keeps your data in a new place since v0.18.0:\n\n"
        f"    {DATA_DIR}\n\n"
        "Your playlists, exceptions, settings, pictures, logs and backups were copied there\n"
        "the first time v0.18.0 started. The old copies in this folder are no longer used.\n"
        "You can delete them once you've checked the app shows your playlists:\n\n"
        "    playlists.json, exceptions.json, track_failures.json, ui_settings.json\n"
        "    (and their .bak files), and the logs, playlist_icons and backups folders.\n\n"
        "Your music files were not moved.\n"
    )
    try:
        with open(os.path.join(old_dir, MOVED_NOTE), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass  # only a courtesy note


def migrate_legacy_data() -> Optional[str]:
    """Copy data left in APP_DIR by a version before v0.18.0 into DATA_DIR,
    once (see the module docstring). Raises on a real failure (an
    interrupted run is finished on the next call); prepare_data_dir()
    catches that. Returns a note for the log, or None if nothing was done."""
    if _same_folder(DATA_DIR, APP_DIR):
        return None
    state = _read_state()
    if state and state.get("state") == "done":
        return None
    staging = data_path(MIGRATION_STAGING)

    if not state or state.get("state") != "moving":
        items = [n for n in LEGACY_FILES + LEGACY_FOLDERS if os.path.exists(os.path.join(APP_DIR, n))]
        if not any(n in _MAIN_FILES for n in items):
            return None  # no data here to move (a new install)
        if any(os.path.exists(data_path(n)) for n in _MAIN_FILES):
            _write_state({"state": "done", "skipped": True, "from": APP_DIR, "at": _now()})
            return (f"Data was found both in {APP_DIR} and in {DATA_DIR}. The data folder is used; "
                    f"the files in the app folder were left alone.")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        for name in items:
            src, dst = os.path.join(APP_DIR, name), os.path.join(staging, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        _point_icons_at(os.path.join(staging, "ui_settings.json"),
                        os.path.join(APP_DIR, "playlist_icons"), data_path("playlist_icons"))
        state = {"state": "moving", "from": APP_DIR, "items": items, "at": _now()}
        _write_state(state)

    for name in state.get("items", []):
        src = os.path.join(staging, name)
        if os.path.exists(src):  # not moved yet (an earlier run may have stopped part-way)
            _move_into(src, data_path(name))
    shutil.rmtree(staging, ignore_errors=True)
    old_dir = state.get("from") or APP_DIR
    _write_moved_note(old_dir)
    state["state"] = "done"
    state["done_at"] = _now()
    _write_state(state)
    return (f"Moved your data from {old_dir} to {DATA_DIR} "
            f"({', '.join(state.get('items', []))}). The old copies were left in place.")
