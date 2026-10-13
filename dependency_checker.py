"""
Mud Music Manager - Dependency Checker

Checks whether the Python packages this project depends on have newer
versions available on PyPI, and can update them via pip. Checked once
when the app starts; the result is cached by the caller (main.py) for
the rest of the session, so opening the menu repeatedly doesn't keep
hitting the network.

Deliberately does NOT check or attempt to update the Python interpreter
itself -- that's a much riskier, more manual operation (can affect other
things on the system) that doesn't belong in an automated in-app flow.
This only ever touches the packages listed in TRACKED_PACKAGES, via pip,
in the current Python environment.

v0.19.1 (Checklist item 60c): in a packaged build (BUNDLED) the libraries
are part of the program and there is no pip, so nothing is checked or
updated here: check_all() returns no statuses and the callers say the
libraries come with each app release (app_update.py tells the user about
those). BUNDLED_NOTE is the wording they use.
"""

import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from typing import List, Optional

import requests
from packaging.version import InvalidVersion, Version

import app_paths
import debug_console
import logger

BUNDLED = app_paths.FROZEN
BUNDLED_NOTE = "Libraries are built into this version and updated with each app release"

TRACKED_PACKAGES = [
    "SpotipyFree",
    "ytmusicapi",
    "yt-dlp",
    "mutagen",
    "requests",
    "syncedlyrics",
]

PYPI_TIMEOUT_SECONDS = 5


@dataclass
class PackageStatus:
    name: str
    installed_version: Optional[str]
    latest_version: Optional[str]
    outdated: bool
    check_error: Optional[str] = None  # set if status couldn't be fully determined


def _get_installed_version(package_name: str) -> Optional[str]:
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return None


def _get_latest_version(package_name: str) -> Optional[str]:
    try:
        response = requests.get(
            f"https://pypi.org/pypi/{package_name}/json", timeout=PYPI_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return response.json()["info"]["version"]
    except Exception as e:
        logger.log_error(f"Failed to check latest PyPI version for {package_name}: {e}")
        return None


def check_all() -> List[PackageStatus]:
    """
    Check every tracked package's installed vs. latest PyPI version.
    A network failure or missing package for one entry doesn't stop the
    others from being checked -- each package's status independently
    reflects what could actually be determined.

    In a packaged build (BUNDLED) returns [] without asking PyPI.
    """
    if BUNDLED:
        return []
    statuses = []
    for package_name in TRACKED_PACKAGES:
        installed = _get_installed_version(package_name)
        latest = _get_latest_version(package_name)

        outdated = False
        check_error = None
        if installed is None:
            check_error = "not installed / not found"
        elif latest is None:
            check_error = "could not reach PyPI to check"
        else:
            try:
                outdated = Version(installed) < Version(latest)
            except InvalidVersion:
                # Non-standard version string on one side -- fall back to
                # a plain inequality rather than silently assuming it's fine.
                outdated = installed != latest

        statuses.append(PackageStatus(
            name=package_name, installed_version=installed, latest_version=latest,
            outdated=outdated, check_error=check_error,
        ))

    return statuses


def any_outdated(statuses: List[PackageStatus]) -> bool:
    return any(s.outdated for s in statuses)


def _run_pip_install(package_name: str) -> subprocess.CompletedProcess:
    """
    Run `pip install -U <package>`. If pip refuses because the
    environment is "externally managed" (PEP 668 -- common on some
    Linux Python installs, not on the standard Windows/Mac installer),
    automatically retry once with --break-system-packages, which is the
    standard, intentional way to opt into a direct install on those
    systems. Raises CalledProcessError if both attempts fail.
    """
    # v0.16.3: with no console (pythonw.exe) use its python.exe sibling and keep
    # pip's own console window from flashing up.
    base_cmd = [debug_console.console_python_executable(), "-m", "pip", "install", "-U", package_name]
    flags = debug_console.hidden_window_flags()
    try:
        return subprocess.run(base_cmd, check=True, capture_output=True, text=True, creationflags=flags)
    except subprocess.CalledProcessError as e:
        output = (e.stderr or "") + (e.stdout or "")
        if "externally-managed-environment" in output:
            return subprocess.run(
                base_cmd + ["--break-system-packages"], check=True, capture_output=True, text=True,
                creationflags=flags,
            )
        raise


def update_outdated(statuses: List[PackageStatus]) -> List[str]:
    """
    Runs `pip install -U <package>` for every package currently flagged
    as outdated. Returns one human-readable result line per package
    attempted (empty list if nothing was outdated).

    NOTE: this process has already imported the old versions of these
    packages, so an update here won't take effect until the application
    is restarted -- callers should tell the user that.
    """
    if BUNDLED:
        return [BUNDLED_NOTE + "."]
    results = []
    outdated = [s for s in statuses if s.outdated]

    for status in outdated:
        try:
            _run_pip_install(status.name)
            results.append(f"{status.name}: updated to the latest version.")
            logger.log_event(f"Updated package {status.name} via pip")
        except subprocess.CalledProcessError as e:
            output = (e.stderr or e.stdout or str(e)).strip()
            error_detail = output.splitlines()[-1] if output else str(e)
            results.append(f"{status.name}: update failed -- {error_detail}")
            logger.log_error(f"Failed to update package {status.name} via pip: {error_detail}")

    return results
