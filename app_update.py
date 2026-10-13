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
never raises: every problem ends up in AppUpdateStatus.error.
"""

import os
from dataclasses import dataclass
from typing import Optional

import requests
from packaging.version import InvalidVersion, Version

import app_info
import logger

REPO_ENV = "MMM_UPDATE_REPO"
TIMEOUT_SECONDS = 6

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
    url = f"https://api.github.com/repos/{repo()}/releases/latest"
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
    mine = parse_version(current)
    status.outdated = mine is None or latest > mine
    logger.log_event(f"App update check: this copy v{current}, latest release v{status.latest}"
                     f"{' -- update available' if status.outdated else ''}")
    return status
