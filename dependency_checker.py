"""
Mud Music Manager - Dependency Checker

The libraries this project uses: requirements.txt and everything those
libraries need in turn (installed_libraries(), ~35-40 of them), with their
versions and whether each has compiled parts.

Used by:
  - packaging\\build.ps1: write_bundled_libraries() records the build's
    libraries in _internal\\bundled_libraries.json (component_updates reads
    it to drop older components after an app update);
  - the self-test: every library the app needs is there;
  - .github/workflows/release.yml (packaging\\release_check.py) compares the
    latest release's bundled versions with PyPI to decide when to publish a
    new release.

History: up to v0.27.0 this module compared a hand-kept list of six
libraries with PyPI and ran `pip install -U` on request (source copies
only). v0.28.0 (Checklist item 60h) first made it check every library and
pick versions that fit together; the same day Michael chose to update by
installing whole releases instead (each release bundles the newest of
everything), so the per-library checking and pip updating were removed.
Libraries missing for a new Python (running from source) are installed at
start-up by bootstrap.py.

Deliberately never installs or upgrades the Python interpreter itself.
"""

import json
import platform
import re
from importlib import metadata
from typing import Dict, List, Set, Tuple

from packaging.requirements import InvalidRequirement, Requirement

import app_paths

BUNDLED = app_paths.FROZEN
BUNDLED_NOTE = "Libraries are built into this version and updated with each app release"
BUNDLED_LIBRARIES = "bundled_libraries.json"


def norm(name: str) -> str:
    """PyPI's normalised project name: 'SpotipyFree' -> 'spotipyfree', 'yt_dlp' -> 'yt-dlp'."""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def _requirements() -> List[Requirement]:
    """The entries of requirements.txt."""
    import ui_libraries
    out = []
    try:
        lines = ui_libraries.REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = ui_libraries.requirement_names()
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            out.append(Requirement(line))
        except InvalidRequirement:
            continue
    return out


def _marker_ok(req: Requirement, extras: Set[str] = frozenset()) -> bool:
    """Does this requirement apply here (with these extras asked for)?"""
    if req.marker is None:
        return True
    try:
        return any(req.marker.evaluate({"extra": e}) for e in ({""} | set(extras)))
    except Exception:
        return False


def _is_pure(dist) -> bool:
    try:
        text = dist.read_text("WHEEL") or ""
    except Exception:
        text = ""
    m = re.search(r"Root-Is-Purelib:\s*(\w+)", text)
    return bool(m) and m.group(1).lower() == "true"


def _closure_from_environment() -> Dict[str, dict]:
    """requirements.txt and everything it needs, as installed in this Python."""
    out: Dict[str, dict] = {}
    queue: List[Tuple[str, bool, Set[str]]] = [(r.name, True, set(r.extras)) for r in _requirements()]
    while queue:
        name, direct, extras = queue.pop(0)
        key = norm(name)
        if key in out:
            out[key]["direct"] = out[key]["direct"] or direct
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            out[key] = {"name": name, "version": None, "pure": True, "requires": [], "requires_python": "",
                        "direct": direct}
            continue
        reqs = list(dist.requires or [])
        out[key] = {"name": dist.metadata["Name"] or name, "version": dist.version, "pure": _is_pure(dist),
                    "requires": reqs, "requires_python": dist.metadata.get("Requires-Python") or "",
                    "direct": direct}
        for r in reqs:
            try:
                req = Requirement(r)
            except InvalidRequirement:
                continue
            if _marker_ok(req, extras):
                queue.append((req.name, False, set(req.extras)))
    return out


def write_bundled_libraries(path: str) -> int:
    """packaging\\build.ps1: record the build's libraries (run in its build
    environment). Returns how many."""
    libs = _closure_from_environment()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"python": platform.python_version(), "libraries": libs}, f, indent=1)
    return len(libs)


def _bundled_libraries() -> Dict[str, dict]:
    try:
        with open(app_paths.resource_path(BUNDLED_LIBRARIES), encoding="utf-8-sig") as f:
            libs = json.load(f).get("libraries") or {}
    except (OSError, ValueError, AttributeError):
        return _closure_from_environment()  # a build made before v0.28.0: what its metadata shows
    out = {}
    for key, lib in libs.items():
        lib = dict(lib)
        try:  # a version an earlier copy's component update put first on sys.path
            dist = metadata.distribution(lib["name"])
            if dist.version != lib.get("version"):
                lib.update(version=dist.version, requires=list(dist.requires or []))
        except metadata.PackageNotFoundError:
            pass
        out[key] = lib
    return out


def installed_libraries() -> Dict[str, dict]:
    """Every library the app uses: {key: {"name", "version", "pure",
    "requires", "requires_python", "direct"}} (version None: not installed)."""
    return _bundled_libraries() if BUNDLED else _closure_from_environment()
