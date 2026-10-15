"""
Mud Music Manager - Keeping the bundled components current (v0.22.0, Checklist item 60g)

Michael's requirement (2026-10-13): everything the app uses must keep
updating, in the installed and portable app too, not only through new app
releases. The parts that break when they get old are updated between
releases here:

  yt-dlp       downloads from YouTube etc.; YouTube changes often break it
  yt-dlp-ejs   the scripts yt-dlp runs in Deno for YouTube's checks; always the
               exact version the chosen yt-dlp release asks for
  Deno         runs those scripts
  FFmpeg       converts audio and cover art (ffmpeg.exe + ffprobe.exe)

Every other library has compiled parts that can't be swapped safely inside an
installed app; those (and these four too) are refreshed by every new app
release, whose build always takes the newest versions (packaging\\build.ps1).

Where: in every copy since v0.22.2 (Michael found Deno / FFmpeg weren't shown
or updated when running from source). Running from source, Deno and FFmpeg are
updated the same way (the downloaded ones are used instead of the PC's), while
yt-dlp / yt-dlp-ejs stay with pip (python_enabled(): packaged app only; a copy
in the data folder would shadow pip's) and Settings labels them "via pip".

How:
  - check_and_stage() (a background task at launch, at most every 12 hours,
    and when Check For Updates is pressed) asks the official sources for the
    newest versions: PyPI (yt-dlp, yt-dlp-ejs), Deno's GitHub releases, and
    gyan.dev's FFmpeg "essentials" release build. Each download is checked
    against its published SHA-256.
  - Newer versions are assembled with the current ones in a staging folder
    (<data folder>/components/.staging) and TESTED there: the app's own
    self-test (`--selftest`, including a real test download) runs against the
    staging folder (MMM_COMPONENTS_DIR). Only if it passes is the staging
    folder marked ready; otherwise it is deleted and those versions are not
    tried again (a newer one will be).
  - activate(), run by main.py at the very start, before anything uses these
    components: switches a ready staging folder in (nothing is running yet, so
    no file is in use), then puts components/python first on sys.path and
    components/tools first on PATH. So an update is used from the next launch.
  - Bundled versions win when newer: packaging\\build.ps1 records the bundled
    versions (_internal\\bundled_versions.json); a component that is not newer
    than its bundled copy is left out (and dropped), e.g. after an app update.
  - reset_to_built_in() (Settings -> Updates -> Use built-in versions) removes
    the components at the next launch.

State: <data folder>/components/components.json
  {"active": {name: version}, "failed": {name: [versions]}, "last_checked": t,
   "staged": "ready" | null, "reset": bool}

download_latest_tools() is shared with packaging\\build.ps1, which bundles the
newest FFmpeg and Deno the same way.

v0.28.0 (Checklist item 60h, Michael 2026-10-14): the app no longer
downloads single components at all. Updating means installing the newest
release, which bundles the newest Python, libraries, Deno and FFmpeg
(.github/workflows/release.yml publishes one by itself when yt-dlp or
anything else bundled has a newer version; app_update / update_scan install
it). What stays here: activate() still switches in a staging folder an
earlier version left ready and drops components that the program's own
copies have caught up with (an app update brings newer ones; by
bundled_versions.json and bundled_libraries.json, any library by its
wheel's RECORD, _remove_dist()), so a copy updated from v0.22-v0.27 ends up
on its release's versions; reset_to_built_in() / describe() for Settings;
current_versions(); and latest_versions() / download_latest_tools(), which
packaging\build.ps1 uses to bundle the newest Deno and FFmpeg.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from importlib import metadata
from typing import Dict, List, Optional

import app_paths

ENABLE_ENV = "MMM_COMPONENTS_ENABLE"   # "1": also when running from source (tests)
TRY_ENV = "MMM_COMPONENTS_DIR"         # use this folder instead (the self-test of staged components)
TIMEOUT = 20

NAMES = ("yt-dlp", "yt-dlp-ejs", "deno", "ffmpeg")      # shown by name in Settings
TOOLS = ("deno", "ffmpeg")
PY_PACKAGES = {"yt-dlp": "yt_dlp", "yt-dlp-ejs": "yt_dlp_ejs"}
FFMPEG_VERSION_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
DENO_RELEASE_API = "https://api.github.com/repos/denoland/deno/releases/latest"
DENO_ASSET = "deno-x86_64-pc-windows-msvc.zip"
BUNDLED_VERSIONS = "bundled_versions.json"
BUNDLED_LIBRARIES = "bundled_libraries.json"   # v0.28.0: dependency_checker.write_bundled_libraries()
_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


class ComponentError(Exception):
    pass


# ---------------------------------------------------------------------------
# Folders and state
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """Component updates are on everywhere since v0.22.2: Deno and FFmpeg in
    every copy (Michael: everything must keep updating; the source copy used
    to rely on the PC's own copies, never updated by the app)."""
    return True


def python_enabled() -> bool:
    """Libraries are only replaced here in the packaged app. Running from
    source, pip installs and updates them (v0.28.0: downloaded by the update
    scan, installed at the next launch); a copy in the data folder would
    shadow pip's."""
    return app_paths.FROZEN or os.environ.get(ENABLE_ENV) == "1"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def components_dir() -> str:
    return app_paths.data_path("components")


def _state_path() -> str:
    return os.path.join(components_dir(), "components.json")


def load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(components_dir(), exist_ok=True)
    tmp = _state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, _state_path())


def _vkey(v: str):
    """A comparable version: '2026.08.19', '2.9.7', '9.0.2'."""
    from packaging.version import InvalidVersion, Version
    try:
        return Version(str(v).lstrip("vV"))
    except InvalidVersion:
        return Version("0")


def bundled_versions() -> Dict[str, str]:
    """What the program itself ships (written by packaging\\build.ps1): the
    tools and yt-dlp, and (v0.28.0) every library, by normalised name."""
    out = {}
    try:
        with open(app_paths.resource_path(BUNDLED_LIBRARIES), encoding="utf-8-sig") as f:
            libs = json.load(f).get("libraries") or {}
        out.update({_norm(k): str(v["version"]) for k, v in libs.items() if v.get("version")})
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    try:
        with open(app_paths.resource_path(BUNDLED_VERSIONS), encoding="utf-8-sig") as f:  # build.ps1 writes a BOM (v0.28.0 fix)
            data = json.load(f)
        if isinstance(data, dict):
            out.update({k: str(v) for k, v in data.items()})
    except (OSError, ValueError):
        pass
    return out


# ---------------------------------------------------------------------------
# Start-up: switch staged components in, then use them
# ---------------------------------------------------------------------------

def activate() -> Optional[str]:
    """Run once at start-up, before yt-dlp, Deno or FFmpeg are used. Returns a
    note for the session log, or None. Never raises."""
    if not enabled():
        return None
    notes = []
    try:
        trial = os.environ.get(TRY_ENV)
        base = trial or components_dir()
        if not trial:
            state = load_state()
            if state.get("reset"):
                shutil.rmtree(components_dir(), ignore_errors=True)
                return "Components: back to the built-in versions (reset)."
            staging = os.path.join(base, ".staging")
            if state.get("staged") == "ready" and os.path.isdir(staging):
                for part in ("python", "tools"):
                    old = os.path.join(base, part)
                    shutil.rmtree(old, ignore_errors=True)
                    if os.path.isdir(os.path.join(staging, part)):
                        os.replace(os.path.join(staging, part), old)
                state["active"] = state.get("staged_versions", {})
                state["staged"] = None
                state.pop("staged_versions", None)
                shutil.rmtree(staging, ignore_errors=True)
                _save_state(state)
                notes.append("Components updated: " + ", ".join(f"{k} {v}" for k, v in state["active"].items()))
        # v0.28.0: first on sys.path before anything is imported - _drop_outdated()
        # imports `packaging`, itself a library that may have been updated
        py = os.path.join(base, "python")
        if python_enabled() and os.path.isdir(py) and any(os.scandir(py)):
            sys.path.insert(0, py)
        if not trial:
            active = _drop_outdated(base, state)
        else:
            active = _read_versions_file(trial)
        tools = os.path.join(base, "tools")
        if os.path.isdir(tools) and any(os.scandir(tools)):
            parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
            os.environ["PATH"] = os.pathsep.join([tools] + parts)
        if active:
            notes.append("Using updated components: " + ", ".join(f"{k} {v}" for k, v in sorted(active.items())))
    except Exception as e:  # never stop the app from starting
        return f"Components: couldn't apply updates ({type(e).__name__}: {e}); using the built-in versions"
    return "; ".join(notes) or None


def _drop_outdated(base: str, state: dict) -> Dict[str, str]:
    """Remove components that aren't newer than the program's own copies
    (after an app update brought newer ones). Returns what stays active."""
    active = dict(state.get("active") or {})
    bundled = bundled_versions()
    changed = False
    for name in list(active):
        if name in bundled and _vkey(active[name]) <= _vkey(bundled[name]):
            _remove_component(base, name)
            active.pop(name)
            changed = True
    if changed:
        state["active"] = active
        _save_state(state)
    return active


def _remove_component(base: str, name: str) -> None:
    if name == "deno":
        for f in ("deno.exe", "DENO_LICENSE.txt"):
            _rm(os.path.join(base, "tools", f))
    elif name == "ffmpeg":
        for f in ("ffmpeg.exe", "ffprobe.exe", "FFMPEG_LICENSE.txt", "FFMPEG_README.txt"):
            _rm(os.path.join(base, "tools", f))
    else:  # a library (v0.28.0: any of them)
        _remove_dist(os.path.join(base, "python"), name)


def _remove_dist(py_dir: str, name: str) -> None:
    """Remove a library unpacked into py_dir: the files its RECORD lists, the
    folders they leave empty (and their __pycache__), and its .dist-info."""
    if not os.path.isdir(py_dir):
        return
    key = _norm(name)
    for entry in os.listdir(py_dir):
        if not entry.endswith(".dist-info") or _norm(entry[:-len(".dist-info")].rsplit("-", 1)[0]) != key:
            continue
        info = os.path.join(py_dir, entry)
        tops = set()
        try:
            with open(os.path.join(info, "RECORD"), encoding="utf-8") as f:
                rows = f.read().splitlines()
        except OSError:
            rows = []
        for row in rows:
            rel = row.split(",", 1)[0].strip()
            parts = rel.replace("\\", "/").split("/")
            if not rel or rel.startswith("/") or ".." in parts or parts[0].endswith(".dist-info"):
                continue
            tops.add(parts[0])
            _rm(os.path.join(py_dir, *parts))
        for top in tops:
            path = os.path.join(py_dir, top)
            if not os.path.isdir(path):
                continue
            for root, dirs, _files in os.walk(path, topdown=False):
                for d in dirs:
                    if d == "__pycache__":
                        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                try:
                    os.rmdir(root)  # only when empty
                except OSError:
                    pass
        shutil.rmtree(info, ignore_errors=True)


def _read_versions_file(base: str) -> Dict[str, str]:
    try:
        with open(os.path.join(base, "versions.json"), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def reset_to_built_in() -> None:
    """Settings -> Use built-in versions: the components are removed at the
    next launch (they may be in use now)."""
    state = load_state()
    state["reset"] = True
    state["staged"] = None
    _save_state(state)


def describe() -> List[str]:
    """One line per component for Settings: the version in use and where it
    comes from."""
    cur = current_versions()
    state = load_state()
    active = state.get("active") or {}
    lines = []
    for name in NAMES:
        v = cur.get(name) or "not found"
        if name in active and _vkey(active[name]) == _vkey(v):
            origin = "updated"
        elif name in PY_PACKAGES and not python_enabled():
            origin = "via pip"      # running from source: Check For Updates updates it
        elif not app_paths.FROZEN:
            origin = "this PC's"    # running from source: the copy installed on the PC
        else:
            origin = "built in"
        lines.append(f"{name} {v} ({origin})" if name in cur else f"{name}: not found")
    others = sorted(k for k in active if k not in NAMES)  # v0.28.0: every pure-Python library
    if others:
        lines.append("also updated: " + ", ".join(f"{k} {active[k]}" for k in others))
    if state.get("staged") == "ready":
        lines.append("Newer versions are ready and will be used from the next launch.")
    if state.get("reset"):
        lines.append("Back to the built-in versions from the next launch.")
    return lines


# ---------------------------------------------------------------------------
# Versions in use and available
# ---------------------------------------------------------------------------

def _tool_version(exe: str, pattern: str) -> Optional[str]:
    path = shutil.which(exe)
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version" if exe == "deno" else "-version"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=20, creationflags=_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(pattern, out or "")
    return m.group(1) if m else None


_TOOL_PATTERNS = {"deno": r"deno (\d+\.\d+\.\d+)", "ffmpeg": r"ffmpeg version (\d+(?:\.\d+)+)"}


def tool_version(exe: str) -> Optional[str]:
    """The version of the deno / ffmpeg on PATH (the copy in use), or None
    (v0.22.3: also used by Settings -> Project libraries)."""
    return _tool_version(exe, _TOOL_PATTERNS[exe])


def current_versions() -> Dict[str, str]:
    out = {}
    for name in PY_PACKAGES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    deno = _tool_version("deno", r"deno (\d+\.\d+\.\d+)")
    if deno:
        out["deno"] = deno
    ff = _tool_version("ffmpeg", r"ffmpeg version (\d+(?:\.\d+)+)")
    if ff:
        out["ffmpeg"] = ff
    return out


def _get(url: str, **kw):
    import requests
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mud-Music-Manager"}, **kw)
    r.raise_for_status()
    return r


def latest_versions() -> Dict[str, dict]:
    """The newest Deno and FFmpeg and where to get them (packaging/build.ps1
    and packaging/release_check.py; v0.28.0: the app itself no longer
    downloads them)."""
    out = {}
    rel = _get_json_github(DENO_RELEASE_API)
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    if DENO_ASSET in assets and DENO_ASSET + ".sha256sum" in assets:
        out["deno"] = {"version": str(rel.get("tag_name", "")).lstrip("v"), "url": assets[DENO_ASSET],
                       "sha256_url": assets[DENO_ASSET + ".sha256sum"]}
    ffv = _get(FFMPEG_VERSION_URL).text.strip()
    if re.fullmatch(r"\d+(\.\d+)+", ffv):
        out["ffmpeg"] = {"version": ffv, "url": FFMPEG_ZIP_URL, "sha256_url": FFMPEG_ZIP_URL + ".sha256"}
    return out


def _get_json_github(url: str) -> dict:
    import requests
    headers = {"User-Agent": "Mud-Music-Manager", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:  # v0.28.0: the release workflow's token (GitHub's machines share the 60-an-hour anonymous limit)
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(url, timeout=TIMEOUT, headers=headers)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Downloading (checked) and unpacking
# ---------------------------------------------------------------------------

def _download(url: str, dest: str, sha256: str, on_log=None) -> None:
    """Download url to dest and check its SHA-256; raises ComponentError."""
    import requests
    digest = hashlib.sha256()
    try:
        with requests.get(url, stream=True, timeout=TIMEOUT, headers={"User-Agent": "Mud-Music-Manager"}) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(512 * 1024):
                    f.write(chunk)
                    digest.update(chunk)
    except (requests.RequestException, OSError) as e:
        _rm(dest)
        raise ComponentError(f"download failed: {type(e).__name__}")
    if digest.hexdigest().lower() != sha256.lower():
        _rm(dest)
        raise ComponentError("the download didn't match its published fingerprint")
    if on_log:
        on_log(f"  downloaded and checked {os.path.basename(dest)}")


def _published_sha256(url: str) -> str:
    m = re.search(r"\b([0-9a-fA-F]{64})\b", _get(url).text)
    if not m:
        raise ComponentError(f"no fingerprint found at {url}")
    return m.group(1).lower()


def _unpack_deno(zip_path: str, tools_dir: str) -> None:
    os.makedirs(tools_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        with z.open("deno.exe") as src, open(os.path.join(tools_dir, "deno.exe"), "wb") as dst:
            shutil.copyfileobj(src, dst)
    # Deno's zip holds only deno.exe; its licence text ships with the app
    # (the bundled tools folder, or packaging\licenses when running from source).
    for lic in (os.path.join(app_paths.TOOLS_DIR, "DENO_LICENSE.txt"),
                os.path.join(app_paths.APP_DIR, "packaging", "licenses", "DENO_LICENSE.txt")):
        if os.path.isfile(lic) and not os.path.samefile(os.path.dirname(lic), tools_dir):
            shutil.copy2(lic, os.path.join(tools_dir, "DENO_LICENSE.txt"))
            break


def _unpack_ffmpeg(zip_path: str, tools_dir: str) -> None:
    os.makedirs(tools_dir, exist_ok=True)
    wanted = {"bin/ffmpeg.exe": "ffmpeg.exe", "bin/ffprobe.exe": "ffprobe.exe",
              "LICENSE": "FFMPEG_LICENSE.txt", "README.txt": "FFMPEG_README.txt"}
    found = set()
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            tail = member.split("/", 1)[1] if "/" in member else member  # the zip has one top folder
            if tail in wanted:
                with z.open(member) as src, open(os.path.join(tools_dir, wanted[tail]), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                found.add(tail)
    if not {"bin/ffmpeg.exe", "bin/ffprobe.exe"} <= found:
        raise ComponentError("the FFmpeg download doesn't hold ffmpeg.exe and ffprobe.exe")


def download_latest_tools(dest_dir: str, on_log=print) -> Dict[str, str]:
    """The newest Deno and FFmpeg, checked, into dest_dir (packaging\\build.ps1
    uses this to bundle them). Returns their versions."""
    latest = latest_versions()
    os.makedirs(dest_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="mmm_tools_")
    try:
        for name, unpack in (("deno", _unpack_deno), ("ffmpeg", _unpack_ffmpeg)):
            info = latest.get(name)
            if not info:
                raise ComponentError(f"couldn't find the latest {name}")
            z = os.path.join(tmp, f"{name}.zip")
            on_log(f"{name} {info['version']}: downloading {info['url']}")
            _download(info["url"], z, _published_sha256(info["sha256_url"]), on_log)
            unpack(z, dest_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"deno": latest["deno"]["version"], "ffmpeg": latest["ffmpeg"]["version"]}


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
