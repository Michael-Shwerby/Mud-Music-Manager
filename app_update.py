"""
Mud Music Manager - New version check (v0.19.0, Checklist item 60b)

Asks GitHub for the latest published release of Mud Music Manager and
compares its version with this copy's (app_info.VERSION). Run once at
launch beside the library check (dependency_checker.py), and again by
Check For Updates; the terminal menu shows the same result.

This only tells the user a newer version exists, with its release notes
and a link to its download page. Installing it is a later step (Checklist
item 60e, once the installer exists).

How releases are read: GitHub's public REST API,
  GET https://api.github.com/repos/<app_info.GITHUB_REPO>/releases/latest
with no login. "latest" is the newest published release that isn't a
draft or a pre-release. Its tag ("v0.19.0") is the version; its body (the
release notes, Markdown) and html_url (the release page) are shown. A
request without a login is limited to 60 an hour per network, far more
than one per launch needs.

While the repository is private, GitHub answers 404 for it (to an
anonymous caller a private repository doesn't exist). That is reported as
"no public release found", never as an error the user must act on, and
the check starts working by itself once the repository is public.

MMM_UPDATE_REPO (environment variable, "owner/name") points the check at
another repository, for testing against one that has public releases.

Deliberately has NO Qt dependency (the terminal menu uses it too) and
check_latest() never raises: every problem ends up in AppUpdateStatus.error.

One-click update (v0.21.0, Checklist item 60e). A release built by
packaging\\build.ps1 carries MudMusicManager-Setup-<version>.exe and
SHA256SUMS.txt as release files ("assets"); check_latest() records their
download links. In the INSTALLED app (packaged, not portable: can_install())
download_installer() downloads the installer into a temporary folder,
reporting progress, and checks its SHA-256 against SHA256SUMS.txt: a file
whose fingerprint doesn't match is deleted and never run. launch_installer()
then starts it with /SILENT (only its progress window, no questions; it
installs over the existing copy, found by its AppId) and /relaunch=1 (the
installer reopens the app when done); the caller quits the app straight away
(the installer also closes it if needed). The user's data is never touched.
The portable .zip and source copies don't use the installer (it would turn
them into an installed copy): they keep the "Open download page" button.

MMM_UPDATE_API (environment variable) replaces https://api.github.com, for
testing against a local fake release server.

v0.28.0 (Checklist item 60h, Michael 2026-10-14): updating means installing
the newest RELEASE, which carries the newest Python, libraries, Deno and
FFmpeg (built and published automatically by .github/workflows/release.yml),
instead of swapping single libraries. Update & Restart MMM does it:
  - installed app: the installer, as above;
  - portable copy (new): download_update() fetches the release's
    MudMusicManager-Portable-<version>.zip and checks it like the installer;
    launch_portable_update() unpacks it next to this copy and starts a small
    PowerShell script that waits for the app to close, swaps the program's
    files (_internal\\, tools\\ and the files beside the .exe; the old ones
    kept until the new ones are in place, put back on any failure), leaves
    the data in that folder untouched, logs to logs\\update_log.txt and
    opens the app again;
  - running from source: the release page only (a source copy is updated
    with git).
"""

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

import requests
from packaging.version import InvalidVersion, Version

import app_info
import app_paths
import logger

REPO_ENV = "MMM_UPDATE_REPO"
API_ENV = "MMM_UPDATE_API"
TIMEOUT_SECONDS = 6
DOWNLOAD_TIMEOUT_SECONDS = 30            # per network read, not the whole download
INSTALLER_PREFIX = "MudMusicManager-Setup-"
PORTABLE_PREFIX = "MudMusicManager-Portable-"   # v0.28.0
SUMS_NAME = "SHA256SUMS.txt"
DOWNLOAD_FOLDER = os.path.join(tempfile.gettempdir(), "Mud Music Manager update")


class UpdateError(Exception):
    """The update couldn't be downloaded, checked or started; the message
    says why, in words for the user."""

# AppUpdateStatus.error_kind
NO_RELEASE = "no_release"      # 404: no public release yet (or the repo is private)
OFFLINE = "offline"            # couldn't reach GitHub
BAD_ANSWER = "bad_answer"      # GitHub answered, but not with a usable release
RATE_LIMITED = "rate_limited"  # too many requests from this network for now


@dataclass
class AppUpdateStatus:
    current: str
    latest: Optional[str] = None       # "0.19.0" (tag without the "v")
    outdated: bool = False
    notes: str = ""                    # the release notes (Markdown)
    page_url: str = ""                 # the release's page on GitHub
    name: str = ""                     # the release's title
    published: str = ""                # ISO date
    error: Optional[str] = None        # set if the check couldn't tell
    error_kind: Optional[str] = None
    # v0.21.0: the release's installer and checksum file, if it has them
    installer_name: str = ""
    installer_url: str = ""
    installer_size: int = 0
    sums_url: str = ""
    # v0.28.0: the release's portable .zip
    portable_name: str = ""
    portable_url: str = ""
    portable_size: int = 0

    def can_install(self) -> bool:
        """True when Update now can install this release: there is a newer
        release with an installer and a checksum file, and this copy is the
        installed app (packaged, not portable)."""
        return (self.outdated and bool(self.installer_url) and bool(self.sums_url)
                and app_paths.FROZEN and not app_paths.is_portable())

    def can_update_portable(self) -> bool:
        """v0.28.0: a newer release with a portable .zip and a checksum file,
        and this copy is the portable app."""
        return (self.outdated and bool(self.portable_url) and bool(self.sums_url)
                and app_paths.FROZEN and app_paths.is_portable())

    def can_self_update(self) -> bool:
        """Update & Restart MMM can update this copy by itself (v0.28.0)."""
        return self.can_install() or self.can_update_portable()

    def summary(self) -> str:
        """One line for the header, Settings and the terminal menu."""
        if self.outdated:
            return f"Mud Music Manager v{self.latest} available"
        if self.error_kind == NO_RELEASE:
            return "No public release of Mud Music Manager found yet"
        if self.error:
            return "Couldn't check for a new version of Mud Music Manager"
        return f"Mud Music Manager is up to date (v{self.current})"


def repo() -> str:
    return (os.environ.get(REPO_ENV) or app_info.GITHUB_REPO).strip().strip("/")


def releases_page() -> str:
    return f"https://github.com/{repo()}/releases"


def parse_version(tag: str) -> Optional[Version]:
    """'v0.19.0' / '0.19.0' / 'V1.0' -> Version, or None if it isn't one."""
    text = (tag or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    try:
        return Version(text)
    except InvalidVersion:
        return None


def check_latest(current: Optional[str] = None, timeout: float = TIMEOUT_SECONDS) -> AppUpdateStatus:
    """Compare this copy's version with the latest GitHub release. Never raises."""
    current = current or app_info.VERSION
    status = AppUpdateStatus(current=current)
    api = (os.environ.get(API_ENV) or "https://api.github.com").rstrip("/")
    url = f"{api}/repos/{repo()}/releases/latest"
    try:
        response = requests.get(url, timeout=timeout, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Mud-Music-Manager/{current}",
            "X-GitHub-Api-Version": "2022-11-28",
        })
    except requests.RequestException as e:
        status.error, status.error_kind = f"couldn't reach GitHub ({type(e).__name__})", OFFLINE
        logger.log_event(f"App update check: {status.error}")
        return status

    if response.status_code == 404:
        status.error, status.error_kind = "no public release found", NO_RELEASE
        logger.log_event(f"App update check: no public release of {repo()} found "
                         f"(none published yet, or the repository is private)")
        return status
    if response.status_code in (403, 429) and response.headers.get("X-RateLimit-Remaining") == "0":
        status.error, status.error_kind = "GitHub's hourly limit was reached; try again later", RATE_LIMITED
        logger.log_event(f"App update check: {status.error}")
        return status
    if response.status_code != 200:
        status.error, status.error_kind = f"GitHub answered {response.status_code}", BAD_ANSWER
        logger.log_error(f"App update check: GitHub answered {response.status_code} for {url}")
        return status

    try:
        data = response.json()
        tag = str(data.get("tag_name") or "")
    except (ValueError, AttributeError):
        status.error, status.error_kind = "GitHub's answer couldn't be read", BAD_ANSWER
        logger.log_error(f"App update check: unreadable answer from {url}")
        return status
    latest = parse_version(tag)
    if latest is None:
        status.error, status.error_kind = f"the latest release's tag isn't a version ({tag!r})", BAD_ANSWER
        logger.log_error(f"App update check: {status.error}")
        return status

    status.latest = str(latest)
    status.notes = str(data.get("body") or "").strip()
    status.page_url = str(data.get("html_url") or releases_page())
    status.name = str(data.get("name") or tag)
    status.published = str(data.get("published_at") or "")[:10]
    for asset in data.get("assets") or []:  # v0.21.0: the installer and its checksums
        if not isinstance(asset, dict):
            continue
        name, link = str(asset.get("name") or ""), str(asset.get("browser_download_url") or "")
        if name.startswith(INSTALLER_PREFIX) and name.lower().endswith(".exe") and link:
            status.installer_name, status.installer_url = name, link
            status.installer_size = int(asset.get("size") or 0)
        elif name.startswith(PORTABLE_PREFIX) and name.lower().endswith(".zip") and link:  # v0.28.0
            status.portable_name, status.portable_url = name, link
            status.portable_size = int(asset.get("size") or 0)
        elif name == SUMS_NAME and link:
            status.sums_url = link
    mine = parse_version(current)
    status.outdated = mine is None or latest > mine
    logger.log_event(f"App update check: this copy v{current}, latest release v{status.latest}"
                     f"{' -- update available' if status.outdated else ''}"
                     f"{' (installer attached)' if status.installer_url and status.sums_url else ''}")
    return status


# ---------------------------------------------------------------------------
# One-click update (v0.21.0, Checklist item 60e)
# ---------------------------------------------------------------------------

def _expected_sha256(status: AppUpdateStatus, file_name: Optional[str] = None) -> str:
    """A release file's fingerprint (the installer's by default) as listed in
    the release's SHA256SUMS.txt."""
    file_name = file_name or status.installer_name
    try:
        response = requests.get(status.sums_url, timeout=TIMEOUT_SECONDS,
                                headers={"User-Agent": f"Mud-Music-Manager/{status.current}"})
        response.raise_for_status()
    except requests.RequestException as e:
        raise UpdateError(f"Couldn't download the release's checksum file ({type(e).__name__}).")
    for line in response.text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == file_name and len(parts[0]) == 64:
            return parts[0].lower()
    raise UpdateError("The release's checksum file doesn't list its download, so it can't be checked. "
                      "Download it from the release page instead.")


def download_installer(status: AppUpdateStatus,
                       on_progress: Optional[Callable[[int, int], None]] = None,
                       should_cancel: Optional[Callable[[], bool]] = None) -> str:
    """Download the release's installer into DOWNLOAD_FOLDER and check it
    against SHA256SUMS.txt. on_progress(bytes_done, bytes_total) is called as
    it goes (total 0 if unknown); should_cancel() returning True stops it.
    Returns the installer's path. Raises UpdateError (anything downloaded is
    deleted)."""
    name = os.path.basename(status.installer_name or "")
    if not (name.startswith(INSTALLER_PREFIX) and name.lower().endswith(".exe") and status.installer_url):
        raise UpdateError("This release has no installer to download.")
    return _download(status, name, status.installer_url, status.installer_size, on_progress, should_cancel)


def download_update(status: AppUpdateStatus,
                    on_progress: Optional[Callable[[int, int], None]] = None,
                    should_cancel: Optional[Callable[[], bool]] = None) -> str:
    """v0.28.0: what Update & Restart MMM downloads for this copy: the
    installer (installed app) or the portable .zip (portable copy), checked
    against SHA256SUMS.txt. Returns its path; raises UpdateError."""
    if status.can_update_portable():
        name = os.path.basename(status.portable_name or "")
        if not (name.startswith(PORTABLE_PREFIX) and name.lower().endswith(".zip")):
            raise UpdateError("This release has no portable .zip to download.")
        return _download(status, name, status.portable_url, status.portable_size, on_progress, should_cancel)
    return download_installer(status, on_progress, should_cancel)


def _download(status: AppUpdateStatus, name: str, url: str, size: int,
              on_progress: Optional[Callable[[int, int], None]],
              should_cancel: Optional[Callable[[], bool]]) -> str:
    expected = _expected_sha256(status, name)
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    final = os.path.join(DOWNLOAD_FOLDER, name)
    part = final + ".part"
    digest = hashlib.sha256()
    done = 0
    logger.log_event(f"App update: downloading {url}")
    try:
        with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_SECONDS,
                          headers={"User-Agent": f"Mud-Music-Manager/{status.current}"}) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or size or 0)
            with open(part, "wb") as f:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if should_cancel is not None and should_cancel():
                        raise UpdateError("The update was cancelled. Nothing was installed.")
                    if not chunk:
                        continue
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
    except UpdateError:
        _remove(part)
        raise
    except (requests.RequestException, OSError) as e:
        _remove(part)
        logger.log_error(f"App update: download failed: {e}")
        raise UpdateError(f"The download didn't finish ({type(e).__name__}). Nothing was installed; "
                          f"try again later.")
    actual = digest.hexdigest()
    if actual != expected:
        _remove(part)
        logger.log_error(f"App update: {name} fingerprint {actual} doesn't match the release's {expected}; deleted")
        raise UpdateError("The download didn't match the release's fingerprint, so it was deleted and nothing "
                          "was installed. Try again, or download it from the release page.")
    os.replace(part, final)
    logger.log_event(f"App update: {name} downloaded ({done:,} bytes) and its fingerprint matches")
    return final


def launch_installer(path: str) -> None:
    """Start the downloaded installer: /SILENT shows only its progress window
    (no questions), it updates the installed copy in place (same AppId and
    folder), and /relaunch=1 makes it open the app again when done
    (packaging\\installer.iss). The caller must quit the app straight after.
    Raises UpdateError if it can't be started."""
    args = [path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/relaunch=1"]
    if installed_for_all_users():
        # v0.22.0: Michael keeps the installer's "for all users" choice. Such a
        # copy (Program Files) must be updated as an all-users install, so
        # Windows asks for administrator permission; without /ALLUSERS the
        # installer could put a second, per-user copy beside it instead.
        args.append("/ALLUSERS")
    try:
        subprocess.Popen(args, close_fds=True, cwd=os.path.dirname(path),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    except OSError as e:
        logger.log_error(f"App update: couldn't start {path}: {e}")
        raise UpdateError(f"Couldn't start the installer: {e}")
    logger.log_event(f"App update: started {os.path.basename(path)}; closing the app for it")


# ---------------------------------------------------------------------------
# Portable copy (v0.28.0): swap the program files, keep the data
# ---------------------------------------------------------------------------

# The files and folders a portable copy's program is made of (build.ps1's
# program folder). Everything else in that folder is the user's data
# (playlists.json, logs\, components\, ...) and is never touched.
PROGRAM_FOLDERS = ("_internal", "tools")

_SWAP_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$AppPid = {pid}
$App = '{app}'
$New = '{new}'
$Log = '{log}'
$Exe = Join-Path $App '{exe}'
function Say($t) {{ Add-Content -LiteralPath $Log -Value ("[{{0:yyyy-MM-dd HH:mm:ss}}] {{1}}" -f (Get-Date), $t) }}
try {{ Wait-Process -Id $AppPid -Timeout 120 -ErrorAction SilentlyContinue }} catch {{}}
Start-Sleep -Milliseconds 500
Say "Portable update: replacing the program files in $App with $New"
$moved = @()
try {{
    foreach ($d in @({folders})) {{
        $old = Join-Path $App $d
        if (Test-Path -LiteralPath $old) {{
            Rename-Item -LiteralPath $old -NewName ($d + '.old-mmm') ; $moved += $d
        }}
    }}
    Get-ChildItem -LiteralPath $New -Force | ForEach-Object {{
        Copy-Item -LiteralPath $_.FullName -Destination $App -Recurse -Force
    }}
    foreach ($d in $moved) {{ Remove-Item -LiteralPath (Join-Path $App ($d + '.old-mmm')) -Recurse -Force -ErrorAction SilentlyContinue }}
    Remove-Item -LiteralPath (Split-Path $New -Parent) -Recurse -Force -ErrorAction SilentlyContinue
    Say "Portable update: done"
}} catch {{
    Say ("Portable update failed: " + $_.Exception.Message + " -- putting the old program files back")
    foreach ($d in $moved) {{
        $cur = Join-Path $App $d
        if (Test-Path -LiteralPath $cur) {{ Remove-Item -LiteralPath $cur -Recurse -Force -ErrorAction SilentlyContinue }}
        Rename-Item -LiteralPath (Join-Path $App ($d + '.old-mmm')) -NewName $d -ErrorAction SilentlyContinue
    }}
}}
Start-Process -FilePath $Exe -WorkingDirectory $App
"""


def launch_portable_update(zip_path: str) -> None:
    """Unpack the downloaded portable .zip next to the app and start the
    script that swaps the program files once the app has closed, then
    reopens it (see the module notes). The caller must quit the app
    straight after. Raises UpdateError if it can't be prepared or started."""
    import shutil
    import zipfile
    staging = os.path.join(DOWNLOAD_FOLDER, "portable")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            for member in z.namelist():
                parts = member.replace("\\", "/").split("/")
                if member.startswith("/") or ".." in parts or ":" in parts[0]:
                    raise UpdateError("The portable .zip holds an unsafe file name, so it wasn't used.")
            z.extractall(staging)
    except (zipfile.BadZipFile, OSError) as e:
        raise UpdateError(f"The portable .zip couldn't be unpacked ({type(e).__name__}).")
    new = os.path.join(staging, app_info.APP_NAME)
    if not os.path.isfile(os.path.join(new, app_paths.GUI_EXE_NAME)):
        raise UpdateError("The portable .zip doesn't hold Mud Music Manager.exe, so it wasn't used.")
    log_dir = app_paths.data_path("logs")
    os.makedirs(log_dir, exist_ok=True)

    def ps(text: str) -> str:  # a PowerShell single-quoted string
        return str(text).replace("'", "''")

    script = _SWAP_SCRIPT.format(
        pid=os.getpid(), app=ps(app_paths.APP_DIR), new=ps(new), log=ps(os.path.join(log_dir, "update_log.txt")),
        exe=ps(app_paths.GUI_EXE_NAME), folders=", ".join(f"'{ps(d)}'" for d in PROGRAM_FOLDERS))
    script_path = os.path.join(DOWNLOAD_FOLDER, "portable_update.ps1")
    with open(script_path, "w", encoding="utf-8-sig") as f:
        f.write(script)
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                          "-File", script_path], close_fds=True, cwd=DOWNLOAD_FOLDER,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000 | 0x00000200)  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    except OSError as e:
        logger.log_error(f"App update: couldn't start the portable update: {e}")
        raise UpdateError(f"Couldn't start the update: {e}")
    logger.log_event(f"App update: portable update of {app_paths.APP_DIR} started; closing the app for it")


def launch_update(path: str) -> None:
    """v0.28.0: start what download_update() fetched - the installer, or the
    portable swap. The caller must quit the app straight after."""
    if path.lower().endswith(".zip"):
        launch_portable_update(path)
    else:
        launch_installer(path)


APP_ID_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{4974306D-5BCE-4E0F-A115-EED0580EF47D}_is1"


def installed_for_all_users() -> bool:
    """True when this copy was installed with "Install for all users": the
    installer registered it for the whole computer (HKEY_LOCAL_MACHINE) and
    it is running from that install folder (v0.22.0)."""
    if not app_paths.FROZEN or not sys_is_windows():
        return False
    try:
        import winreg
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, APP_ID_KEY, 0, winreg.KEY_READ | view) as k:
                    where = str(winreg.QueryValueEx(k, "InstallLocation")[0]).rstrip("\\/")
                if os.path.normcase(os.path.abspath(where)) == os.path.normcase(app_paths.APP_DIR):
                    return True
            except OSError:
                continue
    except ImportError:
        pass
    return False


def sys_is_windows() -> bool:
    return os.name == "nt"


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
