"""
Mud Music Manager - Copy to a device (v0.26.0, Checklist item 66)

Copies chosen playlists to a folder on another drive -- a USB stick, an SD
card, a phone that shows up as a drive, a synced folder -- so the music can
go where the app can't. Michael's choice (2026-10-13): a **mirror**. After a
copy, each chosen playlist's folder on the device matches the library:
new songs are copied, changed files are copied again, and songs no longer
in the playlist are removed from the device.

Layout: `<device folder>/<playlist folder name>/<the playlist's files>`.
Copied: the playlist's song files (metadata_tagger.audio_files()) and its
`.m3u8` playlist file (playlist_file.py), so the device's player shows
the playlists in order. Not copied: Sync Log.txt, staging leftovers, any
other file.

Safety rules:
  - Removing only happens inside playlist folders the app made on that
    device. They are listed in DEVICE_FILE (`.mudmusicmanager-device.json`
    at the device folder's root). A folder with a playlist's name that was
    already there is only added to (new and changed songs), never removed
    from, and is never marked as the app's.
  - Even in its own folders the app removes only song files and `.m3u8`
    files; any other file (a player's cover cache, .nomedia…) is kept.
    Playlists not chosen this time are left alone.
  - Each file is copied to a temporary name in its folder and then renamed
    into place (os.replace), so an unplugged device never holds a half
    file under a song's name; a leftover `*.mmm-copy` is cleaned next time.
  - Before anything is copied, the free space on the device is checked
    against what is about to be copied (after removals), with 50 MB spare.
  - The device folder may not be the Music folder, a playlist folder, or
    inside / around one.

A file counts as changed when its size differs, or its date is more than
2 s newer than the device copy's (FAT keeps dates to 2 s); copy2 keeps the
library file's date, so an unchanged song is never copied twice.

Only drives and folders with a path can be used: Android phones over USB
are usually MTP devices without a drive letter, which Windows programs
can't write to as a folder (the GUI says so).

No Qt: the GUI runs copy() on a worker thread; checkpoint() lets it pause
and cancel between files.
"""

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import logger
import metadata_tagger
import playlist_file

DEVICE_FILE = ".mudmusicmanager-device.json"
TEMP_SUFFIX = ".mmm-copy"
SPARE_BYTES = 50 * 1024 * 1024
MTIME_SLACK = 2.0


class DeviceCopyError(Exception):
    """The copy can't start (or go on); the message says why, in words."""


@dataclass
class PlaylistPlan:
    name: str
    source: Path
    target: Path
    owned: bool                      # the app made this folder on the device (may remove there)
    to_copy: List[Tuple[Path, Path, int]] = field(default_factory=list)   # (source, target, size)
    to_remove: List[Path] = field(default_factory=list)
    unchanged: int = 0
    replaced: int = 0                # of to_copy: files already there in an older version


@dataclass
class CopyResult:
    name: str
    copied: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    bytes_copied: int = 0
    kept_foreign: bool = False       # a folder the app didn't make: nothing removed there
    errors: List[str] = field(default_factory=list)


def _is_ours_to_copy(path: Path) -> bool:
    if path.suffix.lower() == playlist_file.EXTENSION:
        return playlist_file._is_ours(path)
    return True


def _source_files(folder: Path) -> List[Path]:
    files = list(metadata_tagger.audio_files(folder))
    try:
        files += [p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() == playlist_file.EXTENSION and _is_ours_to_copy(p)]
    except OSError:
        pass
    return files


def _managed_kind(path: Path) -> bool:
    """A file the mirror may remove from the app's own device folder."""
    ext = path.suffix.lower()
    return ext in metadata_tagger.AUDIO_EXTENSIONS or ext == playlist_file.EXTENSION or path.name.endswith(TEMP_SUFFIX)


def read_device_file(dest: Path) -> dict:
    try:
        with open(dest / DEVICE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("folders"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"folders": []}


def _write_device_file(dest: Path, data: dict) -> None:
    tmp = dest / (DEVICE_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, dest / DEVICE_FILE)


def _inside(a: Path, b: Path) -> bool:
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except ValueError:
        return False


def check_destination(dest, sources: List[Path], library: Optional[str]) -> Optional[str]:
    """Why this folder can't be the device folder, or None."""
    dest = Path(dest)
    if not dest.is_dir():
        return f"The folder {dest} doesn't exist (is the device plugged in?)."
    roots = [Path(library)] if library else []
    for p in roots + list(sources):
        if _inside(dest, p) or _inside(p, dest):
            return ("Choose a folder on the device that isn't your Music folder or a playlist folder, "
                    "and doesn't contain them.")
    try:
        probe = dest / (".mmm-write-test" + TEMP_SUFFIX)
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        return f"The app can't write to {dest} ({e.strerror or e})."
    return None


def plan(dest, playlists: List[Tuple[str, Path]]) -> List[PlaylistPlan]:
    """What a copy would do. playlists: (folder name, library folder)."""
    dest = Path(dest)
    owned = set(read_device_file(dest)["folders"])
    plans = []
    for name, source in playlists:
        target = dest / name
        p = PlaylistPlan(name=name, source=Path(source), target=target,
                         owned=name in owned or not target.exists())
        wanted = {}
        for src in _source_files(Path(source)):
            try:
                st = src.stat()
            except OSError:
                continue
            wanted[src.name.lower()] = (src, st)
        existing = {}
        if target.is_dir():
            try:
                existing = {f.name.lower(): f for f in target.iterdir() if f.is_file()}
            except OSError:
                existing = {}
        for key, (src, st) in wanted.items():
            dst = existing.get(key)
            if dst is None:
                p.to_copy.append((src, target / src.name, st.st_size))
                continue
            try:
                dt = dst.stat()
            except OSError:
                dt = None
            if dt is None or dt.st_size != st.st_size or st.st_mtime > dt.st_mtime + MTIME_SLACK:
                p.to_copy.append((src, target / src.name, st.st_size))
                p.replaced += 1
            else:
                p.unchanged += 1
        if p.owned:
            p.to_remove = [f for key, f in existing.items() if key not in wanted and _managed_kind(f)]
        plans.append(p)
    return plans


def space_needed(plans: List[PlaylistPlan]) -> int:
    add = sum(size for p in plans for _s, _d, size in p.to_copy)
    freed = 0
    for p in plans:
        for f in p.to_remove:
            try:
                freed += f.stat().st_size
            except OSError:
                pass
        for _s, d, _size in p.to_copy:
            try:
                freed += d.stat().st_size if d.exists() else 0
            except OSError:
                pass
    return max(0, add - freed)


def size_text(n: float) -> str:
    """'512 bytes', '3.4 MB', '1.2 GB'."""
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def copy(dest, playlists: List[Tuple[str, Path]], checkpoint: Callable[[], None] = lambda: None,
         on_progress: Callable[[str, int, int], None] = lambda text, done, total: None) -> List[CopyResult]:
    """Mirror the playlists to dest. checkpoint() is called between files
    (it may wait, or raise to stop); on_progress(text, files done, files in
    all). Raises DeviceCopyError if it can't start (or the device fails)."""
    dest = Path(dest)
    plans = plan(dest, playlists)
    need = space_needed(plans)
    free = shutil.disk_usage(dest).free
    if need + SPARE_BYTES > free:
        raise DeviceCopyError(f"Not enough space on the device: {size_text(need)} to copy, "
                              f"{size_text(free)} free. Choose fewer playlists or free some space.")
    device = read_device_file(dest)
    owned = set(device["folders"])
    total = sum(len(p.to_copy) + len(p.to_remove) for p in plans)
    done = 0
    results = []
    logger.log_event(f"Copy to device '{dest}': {len(plans)} playlist(s), {total} file(s) to copy or remove, "
                     f"{size_text(need)}.")
    for p in plans:
        r = CopyResult(name=p.name, unchanged=p.unchanged, kept_foreign=not p.owned)
        results.append(r)
        try:
            p.target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise DeviceCopyError(f"Couldn't make the folder {p.target}: {e.strerror or e}")
        if p.owned and p.name not in owned:
            owned.add(p.name)  # recorded before copying, so an interrupted copy still knows it's ours
            device["folders"] = sorted(owned)
            _write_device_file(dest, device)
        for leftover in p.target.glob("*" + TEMP_SUFFIX):  # from an interrupted copy
            try:
                leftover.unlink()
            except OSError:
                pass
        for f in p.to_remove:
            checkpoint()
            on_progress(f"{p.name}: removing “{f.name}”", done, total)
            try:
                f.unlink()
                r.removed += 1
            except OSError as e:
                r.errors.append(f"Couldn't remove {f.name}: {e.strerror or e}")
            done += 1
        replaced = {d.name.lower() for _s, d, _z in p.to_copy if d.exists()}
        for src, dst, size in p.to_copy:
            checkpoint()
            on_progress(f"{p.name}: copying “{src.name}”", done, total)
            tmp = dst.with_name(dst.name + TEMP_SUFFIX)
            try:
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
                if dst.name.lower() in replaced:
                    r.updated += 1
                else:
                    r.copied += 1
                r.bytes_copied += size
            except OSError as e:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                if not dest.exists():
                    raise DeviceCopyError(f"The device was disconnected ({e.strerror or e}).")
                r.errors.append(f"Couldn't copy {src.name}: {e.strerror or e}")
            done += 1
        logger.log_event(f"Copy to device: '{p.name}' -> {r.copied} copied, {r.updated} updated, {r.removed} "
                         f"removed, {r.unchanged} unchanged" + (f", {len(r.errors)} error(s)" if r.errors else "")
                         + (" (a folder the app didn't make: nothing removed)" if r.kept_foreign else ""))
    device["updated"] = time.time()
    device["folders"] = sorted(owned)
    try:
        _write_device_file(dest, device)
    except OSError as e:
        logger.log_error(f"Couldn't update {DEVICE_FILE} on '{dest}': {e}")
    on_progress("Done", total, total)
    return results


def summarize(results: List[CopyResult]) -> Tuple[str, str]:
    """(one line, details) for the progress panel."""
    copied = sum(r.copied for r in results)
    updated = sum(r.updated for r in results)
    removed = sum(r.removed for r in results)
    size = sum(r.bytes_copied for r in results)
    errors = sum(len(r.errors) for r in results)
    if not (copied or updated or removed):
        line = "The device already matches — nothing to copy."
    else:
        line = f"{copied} copied, {updated} updated, {removed} removed ({size_text(size)})"
    if errors:
        line += f" · {errors} file(s) couldn't be copied or removed"
    lines = []
    for r in results:
        lines.append(f"{r.name}: {r.copied} copied, {r.updated} updated, {r.removed} removed, {r.unchanged} unchanged")
        if r.kept_foreign:
            lines.append("    This folder was already on the device and wasn't made by the app, so nothing was "
                         "removed from it (only new and changed songs were copied).")
        lines += [f"    {e}" for e in r.errors]
    return line, "\n".join(lines)
