"""
Mud Music Manager - Installed Library Paths (v0.16.0)

Backs the Settings page's "Project Libraries" section (and the classic
terminal menu's matching Settings option): for every package listed in
requirements.txt, report its installed version and the folder it lives
in on this computer, so that if something behaves inconsistently
Michael can jump straight to the library's files and check them.

Pure Python (no Qt) so the terminal menu can use it too. Read-only --
never installs, updates or modifies anything (updating stays with
dependency_checker.py).
"""

import os
import re
import sys
import sysconfig
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import List, Optional

import app_paths

REQUIREMENTS_PATH = Path(app_paths.resource_path("requirements.txt"))  # v0.18.0: shipped-file folder

# Used only if requirements.txt itself can't be read for some reason.
_FALLBACK_NAMES = [
    "SpotipyFree", "ytmusicapi", "yt-dlp", "mutagen", "requests",
    "syncedlyrics", "packaging", "colorama", "PySide6",
]


@dataclass
class LibraryInfo:
    name: str
    installed: bool
    version: Optional[str] = None
    path: Optional[str] = None  # the library's own folder (or the site-packages folder as a fallback)


def requirement_names() -> List[str]:
    """Package names listed in requirements.txt, in file order."""
    try:
        text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    except OSError:
        return list(_FALLBACK_NAMES)

    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if match:
            names.append(match.group(0))
    return names or list(_FALLBACK_NAMES)


def describe_library(name: str) -> LibraryInfo:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return LibraryInfo(name=name, installed=False)

    base = Path(str(dist.locate_file("")))
    path: Optional[Path] = None

    top_levels = (dist.read_text("top_level.txt") or "").split()
    if not top_levels and dist.files:
        seen = []
        for f in dist.files:
            first = f.parts[0] if f.parts else ""
            if first and not first.endswith((".dist-info", ".egg-info")) and first not in seen and first != "..":
                seen.append(first)
        top_levels = seen

    for top in top_levels:
        for candidate in (base / top, base / f"{top}.py"):
            if candidate.exists():
                path = candidate
                break
        if path:
            break

    return LibraryInfo(
        name=name,
        installed=True,
        version=dist.version,
        path=str(path or base),
    )


def describe_all(include_tools: bool = True) -> List[LibraryInfo]:
    """Every library from requirements.txt, then (v0.22.3) the Deno and FFmpeg
    programs the app uses."""
    infos = [describe_library(n) for n in requirement_names()]
    if include_tools:
        infos += describe_tools()
    return infos


# v0.22.3: Deno and FFmpeg aren't Python packages (pip can't list them), but the
# app depends on them, so the list shows them too: the version and the folder of
# the copy actually in use (bundled, updated in the data folder, or the PC's).
_TOOLS = (("Deno", "deno"), ("FFmpeg", "ffmpeg"))
_tool_cache: Optional[List[LibraryInfo]] = None


def describe_tools(refresh: bool = False) -> List[LibraryInfo]:
    """Deno and FFmpeg as LibraryInfo rows. Reading a version runs the program
    briefly, so the answer is kept for the session (the copy in use only
    changes at a restart); refresh=True reads them again."""
    global _tool_cache
    if _tool_cache is not None and not refresh:
        return list(_tool_cache)
    import shutil
    import component_updates
    infos = []
    for label, exe in _TOOLS:
        found = shutil.which(exe)
        if not found:
            infos.append(LibraryInfo(name=label, installed=False))
            continue
        infos.append(LibraryInfo(name=label, installed=True, version=component_updates.tool_version(exe),
                                 path=os.path.dirname(found)))
    _tool_cache = infos
    return list(infos)


def python_environment() -> dict:
    """Where the interpreter running the app lives, and the folder its
    packages are installed into (where every library above lives)."""
    return {
        "version": sys.version.split()[0],
        "executable": sys.executable,
        "site_packages": sysconfig.get_paths().get("purelib", ""),
    }
