"""
Mud Music Manager - Export / Import user data (v0.17.8, Checklist item 59)

Puts everything the user has set up into one backup file, so a new
install (another computer, or this one after a reinstall) can start with
the same playlists, exceptions and settings:

  - playlists.json: every tracked playlist, album and artist (with its
    watch setting and known releases). The pending-move / pending-rename
    markers of an interrupted operation are left out (they describe this
    computer's folders mid-move).
  - exceptions.json: every exception link.
  - track_failures.json: why songs failed to download and on how many
    syncs, so "No match" / "Unavailable" carry over.
  - ui_settings.json: the user's settings (theme, scan on launch, smooth
    scrolling, prefer explicit, download quality, YouTube sign-in, CMD
    button, remapped keys, which Edit page lists are closed) and custom
    playlist / artist pictures (the image files go in the backup too).
    Left out because they describe this computer, not the user's choices
    (DEVICE_SETTINGS): the cached per-playlist numbers, the last scan time
    and the Sync timing averages. A new install fills them in itself.

The music files are NOT in the backup (they can be gigabytes, and a Sync
downloads them again; a copied Music folder is recognised by its tags as
usual, design doc §3.3).

File format: a .zip holding manifest.json (FORMAT, FORMAT_VERSION, the app
version, when it was made, counts) beside the four JSON files and a
playlist_icons/ folder. Plain JSON on purpose, so a backup can be opened
and read by hand. Picture paths inside it are stored relative
("playlist_icons/<file>") and pointed at this install's playlist_icons
folder on import.

Import REPLACES the data on this computer (no merge). Before anything is
written, the current data is exported to backups/"Before import ….zip"
(SAFETY_DIR, in the data folder since v0.18.0), so an import can always be
undone by importing that file.
The Music folder stays this computer's one if it has one; otherwise the
backup's is used if that folder exists here; otherwise none is set (the
user picks it on the Main Menu). Playlists linked with Connect Existing
keep their folder path; any that doesn't exist here is listed in the
report.

Writes go through each store's own save function (atomic, with .bak),
except the backup zip itself, which is written to a temp file and moved
into place. Deliberately has NO Qt dependency: the terminal menu's
Settings -> Export / Import use it too (v0.17.9; its Import is refused
when the menu was opened from the app's CMD button, MMM_CLI_FROM_GUI). No engine behaviour or protected system (§3.3, §3.4, §3.8) is
involved: this module only copies the data files.
"""

import copy
import json
import os
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import app_paths
import config_manager
import exceptions_manager
import logger
import track_failures
import ui_settings

FORMAT = "mud-music-manager-backup"
FORMAT_VERSION = 1
FILE_EXTENSION = ".zip"

# v0.18.0: in the user's data folder (app_paths.DATA_DIR), not the app folder.
SAFETY_DIR = app_paths.data_path("backups")

MANIFEST_NAME = "manifest.json"
PLAYLISTS_NAME = "playlists.json"
EXCEPTIONS_NAME = "exceptions.json"
FAILURES_NAME = "track_failures.json"
SETTINGS_NAME = "ui_settings.json"
ICONS_FOLDER = "playlist_icons/"

# ui_settings keys that describe this computer rather than the user's
# choices: never exported, and kept as they are on import (except the
# cached numbers and last scan time, which describe the replaced playlists
# and are cleared).
DEVICE_SETTINGS = ("playlist_cache", "last_scan_at", "sync_timing", "scheduled_sync_last", "device_copy_last")  # v0.25.0: last scheduled run

# Keys config_manager adds while a folder rename / Library move is under
# way (crash recovery, §3.8): about this computer's folders, never exported.
_PENDING_KEYS = ("_pending_folder_rename", "_pending_library_move")

# A backup is a few hundred KB; these only stop a damaged or hostile file
# from filling memory when it is opened.
_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_ICON_BYTES = 25 * 1024 * 1024
_ICON_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


class BackupError(Exception):
    """A backup couldn't be written or read; the message says why, in
    words for the user."""


@dataclass
class Counts:
    playlists: int = 0
    albums: int = 0
    artists: int = 0
    exceptions: int = 0
    pictures: int = 0

    def describe(self) -> str:
        """'12 playlists · 3 albums · 1 artist · 25 exceptions · 8 pictures'."""
        parts = []
        for n, one, many in ((self.playlists, "playlist", "playlists"), (self.albums, "album", "albums"),
                             (self.artists, "artist", "artists"), (self.exceptions, "exception", "exceptions"),
                             (self.pictures, "picture", "pictures")):
            parts.append(f"{n} {one if n == 1 else many}")
        return " · ".join(parts)


@dataclass
class Backup:
    """A backup file read and checked by read_backup(), ready to import."""
    path: str
    app_version: str
    created: str                     # ISO time, local
    config: dict
    exceptions: dict
    failures: dict
    settings: dict
    icons: Dict[str, bytes]          # "playlist_icons/<file>" -> image bytes
    counts: Counts

    def created_text(self) -> str:
        try:
            return datetime.fromisoformat(self.created).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return "an unknown date"


@dataclass
class ImportReport:
    counts: Counts
    safety_copy: Optional[str]
    library_path: Optional[str]
    backup_library_path: Optional[str]
    missing_custom_folders: List[str] = field(default_factory=list)  # playlist names
    missing_cookie_file: Optional[str] = None


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

def _count(config: dict, exceptions: dict, pictures: int) -> Counts:
    entries = [e for e in config.get("playlists", []) if isinstance(e, dict)]
    albums = sum(1 for e in entries if e.get("kind") == config_manager.KIND_ALBUM)
    return Counts(
        playlists=len(entries) - albums,
        albums=albums,
        artists=sum(1 for a in config.get("artists", []) if isinstance(a, dict)),
        exceptions=sum(len(v) for v in exceptions.values() if isinstance(v, dict)),
        pictures=pictures,
    )


def default_filename() -> str:
    return f"Mud Music Manager backup {datetime.now().strftime('%Y-%m-%d')}{FILE_EXTENSION}"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _read_config() -> dict:
    """playlists.json as it is on disk (without load_config()'s recovery
    steps, which may write)."""
    if not os.path.exists(config_manager.CONFIG_PATH):
        return copy.deepcopy(config_manager.DEFAULT_CONFIG)
    try:
        with open(config_manager.CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        raise BackupError(f"Couldn't read {config_manager.CONFIG_FILENAME}: {e}")
    if not isinstance(config, dict):
        raise BackupError(f"{config_manager.CONFIG_FILENAME} isn't readable (not a JSON object).")
    for key in _PENDING_KEYS:
        config.pop(key, None)
    return config


def _read_exceptions() -> dict:
    try:
        return exceptions_manager.load_exceptions()
    except SystemExit as e:  # load_exceptions() exits on a corrupt file
        raise BackupError(str(e))


def _unique_name(name: str, used: set) -> str:
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while candidate.lower() in used:
        candidate = f"{stem} ({n}){ext}"
        n += 1
    used.add(candidate.lower())
    return candidate


def export_data(path: str, app_version: str) -> Counts:
    """Write a backup of this install's data to `path` (a .zip). Raises
    BackupError with a readable reason. The file only appears once it is
    complete (temp file, then os.replace)."""
    config = _read_config()
    exceptions = _read_exceptions()
    failures = track_failures.load_all()
    settings = ui_settings.export_values()
    for key in DEVICE_SETTINGS:
        settings.pop(key, None)

    # Pictures: only the files the settings point at, stored by file name.
    icons_out: Dict[str, str] = {}   # arcname -> file on disk
    icon_refs: Dict[str, str] = {}
    used: set = set()
    for key, icon_path in (settings.get("playlist_icons") or {}).items():
        if isinstance(icon_path, str) and os.path.isfile(icon_path):
            arcname = ICONS_FOLDER + _unique_name(os.path.basename(icon_path), used)
            icons_out[arcname] = icon_path
            icon_refs[key] = arcname
    settings["playlist_icons"] = icon_refs

    counts = _count(config, exceptions, len(icons_out))
    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "app_version": app_version,
        "created": datetime.now().isoformat(timespec="seconds"),
        "counts": counts.__dict__,
        "library_path": config.get("library_path"),
    }

    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    tmp_path = path + ".tmp"
    try:
        os.makedirs(folder, exist_ok=True)
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in ((MANIFEST_NAME, manifest), (PLAYLISTS_NAME, config),
                               (EXCEPTIONS_NAME, exceptions), (FAILURES_NAME, failures),
                               (SETTINGS_NAME, settings)):
                zf.writestr(name, json.dumps(data, indent=2, ensure_ascii=False))
            for arcname, src in icons_out.items():
                zf.write(src, arcname)
        with open(tmp_path, "rb+") as f:  # fsync needs a writable handle on Windows
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        logger.log_error(f"Export: couldn't write '{path}': {e}")
        raise BackupError(f"Couldn't write the backup file: {e}")

    logger.log_event(f"Exported user data to '{path}' ({counts.describe()}).")
    return counts


# ---------------------------------------------------------------------------
# Reading a backup
# ---------------------------------------------------------------------------

def _read_member(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise BackupError(f"The backup is missing {name}.")
    if info.file_size > limit:
        raise BackupError(f"{name} in the backup is too large to be real ({info.file_size:,} bytes).")
    return zf.read(info)


def _read_json(zf: zipfile.ZipFile, name: str):
    try:
        return json.loads(_read_member(zf, name, _MAX_JSON_BYTES).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise BackupError(f"{name} in the backup is damaged: {e}")


def _check_config(config) -> dict:
    if not isinstance(config, dict) or not isinstance(config.get("playlists", []), list) \
            or not isinstance(config.get("artists", []), list):
        raise BackupError("The playlists in the backup aren't in the expected form.")
    for entry in config.get("playlists", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("folder_name"), str) \
                or not entry["folder_name"].strip():
            raise BackupError("A playlist in the backup has no name.")
        entry.setdefault("platform", config_manager.PLATFORM_SPOTIFY)
        if not config_manager.get_playlist_source_id(entry):
            raise BackupError(f"The playlist '{entry['folder_name']}' in the backup has no link.")
    if any(not isinstance(a, dict) for a in config.get("artists", [])):
        raise BackupError("An artist in the backup isn't in the expected form.")
    for key in _PENDING_KEYS:
        config.pop(key, None)
    config.setdefault("playlists", [])
    config.setdefault("artists", [])
    return config


def _check_exceptions(data) -> dict:
    if not isinstance(data, dict) or any(
            not isinstance(v, dict) or any(not isinstance(x, str) for x in v.values()) for v in data.values()):
        raise BackupError("The exceptions in the backup aren't in the expected form.")
    return data


def read_backup(path: str) -> Backup:
    """Open and check a backup file. Nothing is written. Raises
    BackupError with a readable reason."""
    if not os.path.isfile(path):  # v0.17.9: a typed path (terminal menu) may be wrong
        raise BackupError(f"There's no file at {path}.")
    if not zipfile.is_zipfile(path):
        raise BackupError("That file isn't a Mud Music Manager backup (it isn't a .zip file).")
    try:
        with zipfile.ZipFile(path, "r") as zf:
            manifest = _read_json(zf, MANIFEST_NAME) if MANIFEST_NAME in zf.namelist() else None
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise BackupError("That file isn't a Mud Music Manager backup.")
            version = manifest.get("format_version")
            if not isinstance(version, int) or version > FORMAT_VERSION:
                raise BackupError(
                    f"This backup was made by a newer Mud Music Manager "
                    f"(v{manifest.get('app_version', '?')}). Update the app, then import it again.")

            config = _check_config(_read_json(zf, PLAYLISTS_NAME))
            exceptions = _check_exceptions(_read_json(zf, EXCEPTIONS_NAME))
            failures = _read_json(zf, FAILURES_NAME)
            if not isinstance(failures, dict):
                failures = {}
            failures = {k: v for k, v in failures.items() if isinstance(v, dict)}
            settings = _read_json(zf, SETTINGS_NAME)
            if not isinstance(settings, dict):
                raise BackupError("The settings in the backup aren't in the expected form.")

            # Pictures: only names the settings point at, inside
            # playlist_icons/, with an image extension and no folders.
            icons: Dict[str, bytes] = {}
            refs = settings.get("playlist_icons")
            refs = refs if isinstance(refs, dict) else {}
            names = set(zf.namelist())
            kept_refs = {}
            for key, arcname in refs.items():
                if not isinstance(arcname, str) or not arcname.startswith(ICONS_FOLDER):
                    continue
                file_name = arcname[len(ICONS_FOLDER):]
                if not file_name or file_name != os.path.basename(file_name) or "\\" in file_name \
                        or file_name in (".", "..") or not file_name.lower().endswith(_ICON_EXTENSIONS) \
                        or arcname not in names:
                    continue
                if arcname not in icons:
                    icons[arcname] = _read_member(zf, arcname, _MAX_ICON_BYTES)
                kept_refs[key] = arcname
            settings["playlist_icons"] = kept_refs
    except (OSError, zipfile.BadZipFile) as e:
        raise BackupError(f"Couldn't read the backup file: {e}")

    return Backup(
        path=path,
        app_version=str(manifest.get("app_version") or "?"),
        created=str(manifest.get("created") or ""),
        config=config,
        exceptions=exceptions,
        failures=failures,
        settings=settings,
        icons=icons,
        counts=_count(config, exceptions, len(icons)),
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def library_path_after_import(backup: Backup) -> Optional[str]:
    """The Music folder this install uses after importing `backup`: its
    current one if set; else the backup's, if that folder exists here;
    else None (set it on the Main Menu)."""
    try:
        current = _read_config().get("library_path")
    except BackupError:
        current = None
    if current:
        return current
    theirs = backup.config.get("library_path")
    if isinstance(theirs, str) and theirs and os.path.isdir(theirs):
        return theirs
    return None


def _write_icon(path: str, data: bytes) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def import_backup(backup: Backup, app_version: str) -> ImportReport:
    """Replace this install's playlists, exceptions, failure history and
    settings with the backup's. A safety copy of the current data is
    exported first; if that fails nothing is changed. Raises BackupError.
    Callers must reload what they hold in memory afterwards (the GUI's
    config dict, keymap, theme); ui_settings is reloaded here."""
    os.makedirs(SAFETY_DIR, exist_ok=True)
    safety = os.path.join(SAFETY_DIR, f"Before import {datetime.now().strftime('%Y-%m-%d %H-%M-%S')}"
                                      f"{FILE_EXTENSION}")
    try:
        export_data(safety, app_version)
    except BackupError as e:
        raise BackupError(f"Nothing was imported: the current data couldn't be saved first ({e}).")

    library_path = library_path_after_import(backup)
    config = copy.deepcopy(backup.config)
    config["library_path"] = library_path

    settings = copy.deepcopy(backup.settings)
    step = "pictures"
    try:
        icon_paths: Dict[str, str] = {}
        if backup.icons:
            os.makedirs(ui_settings.ICONS_DIR, exist_ok=True)
        for arcname, data in backup.icons.items():
            dest = os.path.join(ui_settings.ICONS_DIR, arcname[len(ICONS_FOLDER):])
            _write_icon(dest, data)
            icon_paths[arcname] = dest
        settings["playlist_icons"] = {key: icon_paths[arc] for key, arc in settings["playlist_icons"].items()
                                      if arc in icon_paths}

        step = config_manager.CONFIG_FILENAME
        config_manager.save_config(config)
        step = exceptions_manager.EXCEPTIONS_FILENAME
        exceptions_manager.save_exceptions(backup.exceptions)
        step = track_failures.FAILURES_FILENAME
        track_failures.replace_all(backup.failures)
        step = ui_settings.SETTINGS_FILENAME
        ui_settings.import_values(settings, keep=DEVICE_SETTINGS, clear=("playlist_cache", "last_scan_at"))
    except OSError as e:
        logger.log_error(f"Import from '{backup.path}' failed while writing {step}: {e}")
        raise BackupError(
            f"The import stopped while writing {step}: {e}\n\nYour data from before the import is saved "
            f"in:\n{safety}\nImport that file to put everything back.")

    missing = [e["folder_name"] for e in config["playlists"]
               if e.get("custom_path") and not os.path.isdir(e["custom_path"])]
    cookies = settings.get("youtube_cookies") if isinstance(settings.get("youtube_cookies"), dict) else {}
    cookie_file = cookies.get("file") if cookies.get("mode") == "file" else None
    logger.log_event(f"Imported user data from '{backup.path}' ({backup.counts.describe()}); "
                     f"the data from before is saved in '{safety}'.")
    return ImportReport(
        counts=backup.counts,
        safety_copy=safety,
        library_path=library_path,
        backup_library_path=backup.config.get("library_path"),
        missing_custom_folders=missing,
        missing_cookie_file=cookie_file if cookie_file and not os.path.isfile(cookie_file) else None,
    )
