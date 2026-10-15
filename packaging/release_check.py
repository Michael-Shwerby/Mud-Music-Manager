"""
Mud Music Manager - automatic releases: when to release, and what (v0.28.0, Checklist item 60f/60h)

Used by .github/workflows/release.yml (GitHub's machines); can be run by
hand too. Needs `requests` and `packaging`. Michael's plan (2026-10-14):
updating MMM means installing the newest release, and releases keep the
bundled parts current by themselves, so a YouTube change fixed in yt-dlp
reaches users without anyone building by hand.

  python packaging/release_check.py check [--weekly] [--force]
      Decides whether to build a release now. Compares the latest release's
      bundled_versions.json / bundled_libraries.json (attached to every
      release by the workflow) with the newest versions out there:
        every run (daily)  yt-dlp has a newer stable release (YouTube fixes);
                           app_info.VERSION on main is newer than the latest
                           release (a version Michael pushed);
                           the latest release has no bundled_versions.json;
        --weekly           also: FFmpeg newer; Deno in a newer x.y line;
                           Python newer on its 3.x line; a library listed in
                           requirements.txt newer.
      Writes build=true/false, version=X.Y.Z and reason=... to
      $GITHUB_OUTPUT (and prints them).

  python packaging/release_check.py set-version X.Y.Z
      Writes the version into app_info.py (before the build).

  python packaging/release_check.py notes X.Y.Z <new bundled_versions.json> <notes.md>
      After the build: writes the release's notes (what changed, from the
      previous release's bundled versions to this build's) and, for an
      automatic release, adds the same as a section to RELEASE_NOTES.md.
      A version Michael pushed keeps his RELEASE_NOTES.md section as notes.
"""

import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

import requests
from packaging.version import InvalidVersion, Version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("GITHUB_REPOSITORY") or "Michael-Shwerby/Mud-Music-Manager"
API = "https://api.github.com"
TIMEOUT = 30
UA = {"User-Agent": "Mud-Music-Manager-release"}
GH_HEADERS = dict(UA, Accept="application/vnd.github+json")  # only ever sent to api.github.com
if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
    GH_HEADERS["Authorization"] = f"Bearer {os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')}"
LABELS = {"yt-dlp": "yt-dlp", "yt-dlp-ejs": "yt-dlp-ejs", "deno": "Deno", "ffmpeg": "FFmpeg", "python": "Python"}


def _v(text) -> Optional[Version]:
    try:
        return Version(str(text).lstrip("vV"))
    except (InvalidVersion, TypeError):
        return None


def _get(url: str, **kw):
    """Anything but GitHub's API: no token."""
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# What the latest release carries, and what is out there
# ---------------------------------------------------------------------------

def latest_release() -> Optional[dict]:
    r = requests.get(f"{API}/repos/{REPO}/releases/latest", headers=GH_HEADERS, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def release_asset_json(release: Optional[dict], name: str) -> Optional[dict]:
    for a in (release or {}).get("assets") or []:
        if a.get("name") == name:
            return _get(a["browser_download_url"]).json()
    return None


def app_version() -> str:
    with open(os.path.join(ROOT, "app_info.py"), encoding="utf-8") as f:
        return re.search(r'^VERSION = "([0-9.]+)"', f.read(), re.M).group(1)


def pypi_latest(name: str) -> Optional[Version]:
    data = _get(f"https://pypi.org/pypi/{name}/json").json()
    best = None
    for v, files in (data.get("releases") or {}).items():
        ver = _v(v)
        if ver is None or ver.is_prerelease or ver.is_devrelease or not any(not f.get("yanked") for f in files):
            continue
        best = ver if best is None or ver > best else best
    return best


def python_latest_patch(current: str) -> Optional[Version]:
    cur = _v(current)
    rel = _get("https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false").json()
    same = []
    for r in rel:
        m = re.fullmatch(r"Python (3\.\d+\.\d+)", str(r.get("name", "")).strip())
        v = _v(m.group(1)) if m else None
        if v is not None and cur is not None and v.release[:2] == cur.release[:2]:
            same.append(v)
    return max(same) if same else None


def tools_latest() -> Dict[str, Optional[str]]:
    sys.path.insert(0, ROOT)
    import component_updates
    latest = component_updates.latest_versions()
    return {k: (latest.get(k) or {}).get("version") for k in ("deno", "ffmpeg")}


def requirement_names() -> List[str]:
    names = []
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
            if m:
                names.append(m.group(0))
    return names


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def decide(weekly: bool, force: bool) -> Tuple[bool, str, str]:
    """(build?, version, reason)."""
    release = latest_release()
    tag = (release or {}).get("tag_name") or "v0.0.0"
    released, mine = _v(tag), _v(app_version())
    pushed = mine is not None and released is not None and mine > released
    version = str(mine) if pushed else f"{released.major}.{released.minor}.{released.micro + 1}"
    if pushed:
        return True, version, f"v{mine} is on main and not released yet"
    if release is None:
        return True, version, "no release yet"
    old = release_asset_json(release, "bundled_versions.json")
    if old is None:
        return True, version, f"{tag} has no record of its bundled versions"
    reasons = []
    yt = pypi_latest("yt-dlp")
    if yt and _v(old.get("yt-dlp")) and yt > _v(old["yt-dlp"]):
        reasons.append(f"yt-dlp {old['yt-dlp']} -> {yt}")
    if weekly:
        tools = tools_latest()
        if tools.get("ffmpeg") and _v(old.get("ffmpeg")) and _v(tools["ffmpeg"]) > _v(old["ffmpeg"]):
            reasons.append(f"FFmpeg {old['ffmpeg']} -> {tools['ffmpeg']}")
        nd, od = _v(tools.get("deno")), _v(old.get("deno"))
        if nd and od and nd.release[:2] > od.release[:2]:
            reasons.append(f"Deno {old['deno']} -> {tools['deno']}")
        if old.get("python"):
            py = python_latest_patch(old["python"])
            if py and py > _v(old["python"]):
                reasons.append(f"Python {old['python']} -> {py}")
        libs = (release_asset_json(release, "bundled_libraries.json") or {}).get("libraries") or {}
        for name in requirement_names():
            have = _v((libs.get(_norm(name)) or {}).get("version"))
            new = pypi_latest(name) if have else None
            if have and new and new > have and _norm(name) != "yt-dlp":
                reasons.append(f"{name} {have} -> {new}")
    if reasons:
        return True, version, "; ".join(reasons)
    if force:
        return True, version, "built on request"
    return False, version, "nothing newer"


def cmd_check(args: List[str]) -> int:
    weekly = "--weekly" in args
    force = "--force" in args
    build, version, reason = decide(weekly, force)
    lines = [f"build={'true' if build else 'false'}", f"version={version}", f"reason={reason}"]
    print("\n".join(lines))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return 0


# ---------------------------------------------------------------------------
# set-version / notes
# ---------------------------------------------------------------------------

def cmd_set_version(version: str) -> int:
    path = os.path.join(ROOT, "app_info.py")
    text = open(path, encoding="utf-8").read()
    text = re.sub(r'^VERSION = "[0-9.]+"', f'VERSION = "{version}"', text, count=1, flags=re.M)
    open(path, "w", encoding="utf-8", newline="").write(text)
    print(f"app_info.VERSION = {version}")
    return 0


def _section(md: str, version: str) -> Optional[str]:
    m = re.search(rf"^## v{re.escape(version)}\b.*?$(.*?)(?=^## v|\Z)", md, re.S | re.M)
    return m.group(1).strip() if m else None


def cmd_notes(version: str, new_json: str, notes_path: str) -> int:
    with open(new_json, encoding="utf-8-sig") as f:
        new = json.load(f)
    old = release_asset_json(latest_release(), "bundled_versions.json") or {}
    changes = []
    for key, label in LABELS.items():
        if new.get(key) and old.get(key) and str(new[key]) != str(old[key]):
            changes.append(f"- {label} {old[key]} → {new[key]}")
    bundled = ", ".join(f"{LABELS[k]} {new[k]}" for k in LABELS if new.get(k))
    rn_path = os.path.join(ROOT, "RELEASE_NOTES.md")
    rn = open(rn_path, encoding="utf-8").read()
    own = _section(rn, version)
    if own:  # a version Michael pushed: his notes
        body = own + "\n\n" + (("Also updated:\n" + "\n".join(changes) + "\n\n") if changes else "")
    else:   # an automatic release
        body = ("Automatic update: newer versions of the parts Mud Music Manager runs on, so downloads keep "
                "working when YouTube and the other sites change.\n\n" + ("\n".join(changes) + "\n\n" if changes else ""))
        import datetime
        entry = (f"## v{version} ({datetime.date.today():%Y-%m-%d})\n- Automatic update: "
                 + (", ".join(c[2:] for c in changes) if changes else "the newest versions of the bundled parts")
                 + ".\n\n")
        head = rn.index("## v") if "## v" in rn else len(rn)
        open(rn_path, "w", encoding="utf-8", newline="").write(rn[:head] + entry + rn[head:])
    body += f"Runs on {bundled}."
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(body)
    return 0


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "check":
        return cmd_check(argv[1:])
    if argv[0] == "set-version" and len(argv) == 2:
        return cmd_set_version(argv[1])
    if argv[0] == "notes" and len(argv) == 4:
        return cmd_notes(argv[1], argv[2], argv[3])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
