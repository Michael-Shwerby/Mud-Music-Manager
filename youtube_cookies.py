"""
Mud Music Manager - YouTube Cookies (v0.16.42, Checklist item 48)

Signing in to YouTube through browser cookies, used ONLY as a retry:
downloads stay anonymous as before, and only a download (or an exception
link check) that YouTube refuses with "Sign in to confirm your age" or
"Sign in to confirm you're not a bot" is tried once more signed in
(Michael, 2026-10-09). See downloader.download_audio() / probe_video().

The setting (Settings -> YouTube sign-in, stored in ui_settings.json
under "youtube_cookies"):
  - mode "off" (default): never signed in -- exactly the old behaviour.
  - mode "browser": yt-dlp reads the YouTube cookies of an installed
    browser (Firefox, Chrome, Edge, Brave, Opera, Vivaldi, Chromium,
    Whale; Safari on macOS) -- its default profile -- at the moment of
    the retry. Nothing is copied or kept by this app.
  - mode "file": a Netscape-format cookies.txt file the user exported.
    yt-dlp writes cookies back to its cookie file when it finishes, so it
    is handed a private temporary copy (deleted afterwards): the user's
    file is only ever read.

Known limit (Windows): Chrome, Edge and the other Chromium browsers
encrypt their cookies with "app-bound encryption", and keep the cookie
database locked while open, so yt-dlp usually can't read them. The Test
button says so clearly and suggests Firefox or a cookies.txt file.

No cookie value is ever logged or shown -- only counts and cookie-free
messages. No Qt: the terminal menu's syncs use the same setting.
"""

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from typing import Callable, Optional, Tuple

import yt_dlp
from yt_dlp.cookies import SUPPORTED_BROWSERS

import logger
import track_failures
import ui_settings

SETTING_KEY = "youtube_cookies"

MODE_OFF = "off"
MODE_BROWSER = "browser"
MODE_FILE = "file"

DEFAULT_BROWSER = "firefox"

# Shown in this order (Firefox first: it works on Windows).
_BROWSER_LABELS = (
    ("firefox", "Firefox"),
    ("chrome", "Chrome"),
    ("edge", "Edge"),
    ("brave", "Brave"),
    ("opera", "Opera"),
    ("vivaldi", "Vivaldi"),
    ("chromium", "Chromium"),
    ("whale", "Whale"),
    ("safari", "Safari"),
)
CHROMIUM_BROWSERS = {"chrome", "edge", "brave", "opera", "vivaldi", "chromium", "whale"}

# The failure kinds a signed-in retry can fix.
RETRY_KINDS = {track_failures.AGE_RESTRICTED, track_failures.BOT_CHECK}

# Cookies that only exist on a signed-in YouTube / Google account.
_SIGNED_IN_COOKIES = {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID", "LOGIN_INFO",
                      "__Secure-1PSID", "__Secure-3PSID"}


class CookieError(Exception):
    """The cookies couldn't be loaded (browser not found, locked or
    encrypted database, missing / bad cookies.txt). The message is
    already worded for the user."""


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------

def browsers() -> list:
    """[(yt-dlp name, label)] of the browsers yt-dlp can read here."""
    out = []
    for name, label in _BROWSER_LABELS:
        if name not in SUPPORTED_BROWSERS:
            continue
        if name == "safari" and sys.platform != "darwin":
            continue
        out.append((name, label))
    return out


def browser_label(name: str) -> str:
    return dict(_BROWSER_LABELS).get(name, name.title())


def get_setting() -> dict:
    """{"mode", "browser", "file"}, with bad / missing values replaced."""
    raw = ui_settings.get(SETTING_KEY)
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode") if raw.get("mode") in (MODE_OFF, MODE_BROWSER, MODE_FILE) else MODE_OFF
    known = {name for name, _ in browsers()}
    browser = raw.get("browser") if raw.get("browser") in known else DEFAULT_BROWSER
    path = raw.get("file") if isinstance(raw.get("file"), str) else ""
    return {"mode": mode, "browser": browser, "file": path}


def set_setting(mode: Optional[str] = None, browser: Optional[str] = None, file: Optional[str] = None) -> dict:
    """Change any of the three values; returns the new setting."""
    current = get_setting()
    if mode is not None:
        current["mode"] = mode
    if browser is not None:
        current["browser"] = browser
    if file is not None:
        current["file"] = file
    ui_settings.set_value(SETTING_KEY, current)
    logger.log_event(f"YouTube sign-in set to: {describe(current) if is_on(current) else 'Off'}")
    return current


def is_on(setting: Optional[dict] = None) -> bool:
    """Whether a signed-in retry would be tried (a file mode with no
    file chosen counts as off)."""
    s = setting or get_setting()
    if s["mode"] == MODE_BROWSER:
        return True
    return s["mode"] == MODE_FILE and bool(s["file"])


def signature(setting: Optional[dict] = None) -> tuple:
    """Changes whenever the setting does (the link check's cache key)."""
    s = setting or get_setting()
    return (s["mode"], s["browser"], s["file"]) if is_on(s) else (MODE_OFF,)


def describe(setting: Optional[dict] = None) -> str:
    """e.g. "Firefox cookies" / "cookies.txt" for messages."""
    s = setting or get_setting()
    if s["mode"] == MODE_FILE:
        return os.path.basename(s["file"]) or "a cookies.txt file"
    return f"{browser_label(s['browser'])} cookies"


def chromium_note(browser: str) -> str:
    """The Windows limit for a Chromium browser, or ""."""
    if os.name != "nt" or browser not in CHROMIUM_BROWSERS:
        return ""
    return (f"On Windows, {browser_label(browser)} encrypts its cookies in a way yt-dlp usually can't read "
            f"(and locks them while it's open). If Test fails, use Firefox or a cookies.txt file.")


# ---------------------------------------------------------------------------
# Using the cookies
# ---------------------------------------------------------------------------

def _friendly_error(e: Exception, setting: dict) -> str:
    """A cookie-loading error from yt-dlp, worded for the user."""
    text = track_failures.strip_ansi(str(e)).strip()
    if text.startswith("ERROR: "):
        text = text[len("ERROR: "):]
    low = text.lower()
    if setting["mode"] == MODE_FILE:
        if "does not look like a netscape format" in low or "invalid" in low:
            return ("the file isn't a cookies.txt in Netscape format "
                    "(export it with a \"Get cookies.txt\" browser extension)")
        return text or type(e).__name__
    name = browser_label(setting["browser"])
    if "could not find" in low and ("database" in low or "profile" in low):
        return f"{name} doesn't seem to be installed here (or has never been opened)"
    if setting["browser"] in CHROMIUM_BROWSERS and (
            "dpapi" in low or "decrypt" in low or "could not copy" in low or "app-bound" in low
            or "permission denied" in low):
        return (f"{name}'s cookies are locked or encrypted, so they can't be read "
                f"(a known limit on Windows). Use Firefox or a cookies.txt file instead")
    return text or type(e).__name__


@contextmanager
def _cookie_options(setting: dict):
    """yt-dlp options for the chosen cookies. A cookies.txt is copied to a
    private temporary file first and that copy is deleted afterwards."""
    if setting["mode"] == MODE_BROWSER:
        yield {"cookiesfrombrowser": (setting["browser"], None, None, None)}
        return
    source = setting["file"]
    if not source or not os.path.isfile(source):
        raise CookieError(f"the cookies file wasn't found ({source or 'none chosen'})")
    fd, copy = tempfile.mkstemp(prefix="mmm_cookies_", suffix=".txt")
    os.close(fd)
    try:
        try:
            shutil.copyfile(source, copy)
        except OSError as e:
            raise CookieError(f"the cookies file couldn't be read: {e}") from None
        yield {"cookiefile": copy}
    finally:
        try:
            os.remove(copy)
        except OSError:
            pass


def run_signed_in(ydl_opts: dict, action: Callable, setting: Optional[dict] = None):
    """Run action(ydl) with a YoutubeDL that carries the cookies from
    Settings; returns its result. Raises CookieError if the cookies
    couldn't be loaded (nothing was tried), or whatever action raised."""
    setting = setting or get_setting()
    if not is_on(setting):
        raise CookieError("YouTube sign-in is off")
    with _cookie_options(setting) as extra:
        ydl = yt_dlp.YoutubeDL({**ydl_opts, **extra})
        try:
            try:
                ydl.cookiejar  # load now, so a cookie problem isn't mistaken for a download one
            except Exception as e:
                raise CookieError(_friendly_error(e, setting)) from None
            return action(ydl)
        finally:
            try:
                ydl.close()
            except Exception:  # e.g. saving to a copy whose load failed
                pass


def test(setting: Optional[dict] = None) -> Tuple[bool, str]:
    """Settings' Test button: load the cookies and check they hold a
    signed-in YouTube session. (ok, message). Never raises; slow (it
    reads the browser's cookie database), so run it off the GUI thread."""
    setting = setting or get_setting()
    if setting["mode"] == MODE_OFF:
        return False, "YouTube sign-in is off."
    if setting["mode"] == MODE_FILE and not setting["file"]:
        return False, "Choose a cookies.txt file first."
    source = describe(setting)

    def count(ydl):
        names = [c.name for c in ydl.cookiejar if c.domain.lstrip(".").endswith("youtube.com")]
        return len(names), bool(_SIGNED_IN_COOKIES.intersection(names))

    try:
        total, signed_in = run_signed_in({"quiet": True, "no_warnings": True}, count, setting)
    except CookieError as e:
        logger.log_event(f"YouTube sign-in test with {source} failed: {e}")
        return False, f"Couldn't read {source}: {e}."
    except Exception as e:  # never let the Test button crash the page
        logger.log_error(f"YouTube sign-in test with {source} failed: {type(e).__name__}: {e}")
        return False, f"Couldn't read {source}: {type(e).__name__}: {e}"
    logger.log_event(f"YouTube sign-in test with {source}: {total} YouTube cookies, signed in: {signed_in}")
    if not total:
        where = "this file" if setting["mode"] == MODE_FILE else browser_label(setting["browser"])
        return False, f"No YouTube cookies found in {where}. Sign in to youtube.com there first."
    if not signed_in:
        return False, (f"Found {total} YouTube cookies in {source}, but none from a signed-in account. "
                       f"Sign in to youtube.com there, then test again.")
    return True, (f"✓ Signed in: found {total} YouTube cookies in {source}. Age-restricted and "
                  f"bot-checked downloads will be tried once more with them.")
