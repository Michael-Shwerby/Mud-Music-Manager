"""
Mud Music Manager - Update check (v0.28.0, Checklist item 60h)

Michael (2026-10-14): Update & Restart MMM does one thing - check whether a
newer Mud Music Manager release is published and, if so, install it. A
release carries everything the app runs on (Python, every library, Deno,
FFmpeg) at their newest versions: .github/workflows/release.yml builds and
publishes one by itself whenever yt-dlp has a new release (checked daily;
YouTube changes are fixed there) and, weekly, when anything else bundled has
a newer version. So the app no longer updates single libraries or tools.

scan() (at every launch and on Check For Updates, on a worker thread) asks
GitHub for the latest release (app_update.check_latest()) and reads which
versions this copy carries (bundled()). summary() gives the header /
Settings line, the version pill's tone and whether Update & Restart MMM
shows (a newer release this copy can install by itself: the installed app
or the portable copy). details_markdown() is the window the version pill
opens; text_report() the terminal menu's Update.

Running from source the release is only reported, with its page (a source
copy is updated with git); the libraries for a new Python are installed at
start-up by bootstrap.py.
"""

import platform
import time
from dataclasses import dataclass, field
from typing import Dict, List

import app_paths
import app_update
import component_updates
import logger


@dataclass
class UpdateReport:
    app: app_update.AppUpdateStatus
    bundled: Dict[str, str] = field(default_factory=dict)   # what this copy runs on
    checked_at: float = 0.0


@dataclass
class UpdateView:
    message: str
    tone: str            # the version pill: "ok" / "warn" / "neutral"
    restart_ready: bool  # Update & Restart MMM: a newer release this copy installs by itself
    out_of_date: int     # 1 when a newer release exists, else 0


def bundled() -> Dict[str, str]:
    """Python, yt-dlp, Deno and FFmpeg as this copy uses them."""
    out = {"Python": platform.python_version()}
    try:
        cur = component_updates.current_versions()
    except Exception:
        cur = {}
    for key, label in (("yt-dlp", "yt-dlp"), ("deno", "Deno"), ("ffmpeg", "FFmpeg")):
        if cur.get(key):
            out[label] = cur[key]
    return out


def scan() -> UpdateReport:
    """Ask GitHub for the latest release. Never raises."""
    app = app_update.check_latest()
    report = UpdateReport(app=app, bundled=bundled(), checked_at=time.time())
    logger.log_event(f"Update check: {app.summary()}")
    return report


def can_self_update(report: UpdateReport) -> bool:
    return report.app.can_self_update()


def _check_failed(app: app_update.AppUpdateStatus) -> bool:
    return bool(app.error) and app.error_kind != app_update.NO_RELEASE


def summary(report: UpdateReport, preparing: bool = False) -> UpdateView:
    app = report.app
    if preparing:
        msg = "Downloading the update…"
    elif app.outdated:
        msg = f"v{app.latest} available"
    elif _check_failed(app):
        msg = "Couldn't check for updates"
    else:
        msg = "Up to date"
    tone = "warn" if (app.outdated or preparing) else ("neutral" if _check_failed(app) else "ok")
    return UpdateView(message=msg, tone=tone, restart_ready=can_self_update(report) and not preparing,
                      out_of_date=1 if app.outdated else 0)


def _checked_line(report: UpdateReport) -> str:
    when = time.strftime("%H:%M", time.localtime(report.checked_at or time.time()))
    return f"Checked at {when}: Mud Music Manager v{report.app.current}."


def _bundled_line(report: UpdateReport) -> str:
    return "This copy runs on " + ", ".join(f"{k} {v}" for k, v in report.bundled.items()) + "."


def _how(report: UpdateReport) -> str:
    if report.app.can_self_update():
        return "Update & Restart MMM installs it; your playlists, settings and music stay as they are."
    if not app_paths.FROZEN:
        return "Running from source: update this copy with git (git pull), or download the release."
    return "Download it from the release page."


def details_markdown(report: UpdateReport) -> str:
    """The window the version pill opens."""
    app = report.app
    md = [_checked_line(report), ""]
    if app.outdated:
        how = _how(report) + ("" if app.can_self_update() else f" [Release page]({app.page_url})")
        md += ["### Mud Music Manager Update", "", f"- Mud Music Manager {app.current} → {app.latest}", "", how, ""]
    elif _check_failed(app):
        md += [f"Couldn't check: {app.error}.", ""]
    else:
        md += ["**Mud Music Manager is up to date.**", ""]
    md.append(_bundled_line(report))
    if app.outdated and app.notes:
        md += ["", f"### What's new in v{app.latest}", "", app.notes]
    return "\n".join(md)


def text_report(report: UpdateReport) -> str:
    """The terminal menu's Update, as plain text."""
    app = report.app
    lines = [_checked_line(report)]
    if app.outdated:
        lines += [f"Mud Music Manager {app.current} -> {app.latest} is available.", _how(report)]
        if not app.can_self_update():
            lines.append(app.page_url)
    elif _check_failed(app):
        lines.append(f"Couldn't check: {app.error}.")
    else:
        lines.append("Mud Music Manager is up to date.")
    lines.append(_bundled_line(report))
    return "\n".join(lines).replace("→", "->").replace("—", "-").replace("…", "...")


def items(report: UpdateReport) -> List[str]:
    """What is out of date, by name (empty when up to date)."""
    return ["Mud Music Manager"] if report.app.outdated else []
