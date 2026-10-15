"""
Mud Music Manager - Application Identity (v0.17.1; open source since v0.17.5)

Copyright (c) 2026 Michael Xuereb.
Free software under the GNU General Public License v3 or later; see LICENSE.txt.

The one place the app's name, owner, copyright line, Windows identity and
brand-image paths live, so the window, the About panel, the terminal menu
and the documents all say the same thing. No Qt and no third-party
imports: the terminal menu and the launcher can use it too.

Brand images (Checklist item 52), all in assets/:
  - logo_dark.png / logo_light.png: the full logo shown at the top left of
    the window; the one matching the current theme is used.
  - app_icon.png: the emblem, the icon of every window and of the taskbar
    button.
  - app_icon.ico: the same emblem pre-rendered at 16-256 px, for Windows
    shortcuts and any future .exe build.
  - kofi_symbol.png (v0.27.0): Ko-fi's own cup logo (their brand asset,
    unchanged), shown beside the Ko-fi link in the Support window.

v0.18.0: VERSION lives here (was main.py's), so there is one version
number for the whole app; main.VERSION is kept as the same value. The
shipped files (assets, licence texts) are found through
app_paths.RESOURCE_DIR, which is also right in a packaged build.
"""

import os

import app_paths

APP_NAME = "Mud Music Manager"
# The one version number (MAJOR.MINOR.PATCH). It must match the GitHub
# release tag (v0.18.0) and the version in the three project documents.
VERSION = "0.28.0"
# Where releases are published (v0.19.0, app_update.py). The new-version
# check reads this repository's latest GitHub release; it needs the
# repository to be public (a private one looks like it has no release).
GITHUB_REPO = "Michael-Shwerby/Mud-Music-Manager"
# v0.27.0: Michael's Ko-fi page, opened by the Main Menu's Support window.
KOFI_URL = "https://ko-fi.com/michaelshwerby"
OWNER = "Michael Xuereb"
COPYRIGHT_YEAR = "2026"
# v0.17.5 (Checklist item 56): open source under GPL-3.0-or-later (was "All rights reserved").
LICENSE_NAME = "GNU GPL v3 or later"
COPYRIGHT = f"© {COPYRIGHT_YEAR} {OWNER}. Free software under the {LICENSE_NAME}."
COPYRIGHT_ASCII = f"Copyright (c) {COPYRIGHT_YEAR} {OWNER}. Free software under the {LICENSE_NAME}."  # for consoles
# The GPL's "Appropriate Legal Notices" for an interactive program: no warranty, and where the licence is.
WARRANTY_NOTE = "This program comes with ABSOLUTELY NO WARRANTY. You may share and change it under the licence's terms (see License)."
TRADEMARK_NOTE = f"{APP_NAME}™ and its logo are trademarks of {OWNER}; the licence does not grant use of them."
PLATFORM_DISCLAIMER = (
    "Spotify, YouTube, YT Music, SoundCloud, Apple Music and Amazon Music are trademarks of their "
    f"respective owners. {APP_NAME} is not affiliated with, sponsored or endorsed by any of them."
)

# Windows groups taskbar buttons by this ID. Without one the app is grouped
# under pythonw.exe and shows Python's icon. Kept free of the version so a
# future pinned shortcut that carries the same ID keeps matching.
WINDOWS_APP_ID = "MichaelXuereb.MudMusicManager"

BASE_DIR = app_paths.RESOURCE_DIR
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
LOGO_DARK_PATH = os.path.join(ASSETS_DIR, "logo_dark.png")
LOGO_LIGHT_PATH = os.path.join(ASSETS_DIR, "logo_light.png")
ICON_PNG_PATH = os.path.join(ASSETS_DIR, "app_icon.png")
ICON_ICO_PATH = os.path.join(ASSETS_DIR, "app_icon.ico")
KOFI_ICON_PATH = os.path.join(ASSETS_DIR, "kofi_symbol.png")
LICENSE_PATH = os.path.join(BASE_DIR, "LICENSE.txt")
THIRD_PARTY_PATH = os.path.join(BASE_DIR, "THIRD_PARTY_NOTICES.txt")


def logo_path(theme: str) -> str:
    """The full logo for a theme ("dark" or "light")."""
    return LOGO_LIGHT_PATH if theme == "light" else LOGO_DARK_PATH


def set_windows_app_id() -> None:
    """Give this process its own taskbar identity (Windows only), so its
    taskbar button shows the app's icon instead of Python's. Must run
    before the first window is shown. Any failure is ignored: the app then
    just shows under Python, as before."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except Exception:
        pass


def read_text(path: str) -> str:
    """A bundled text file (LICENSE.txt, THIRD_PARTY_NOTICES.txt), or a
    short note if it is missing."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return f"{os.path.basename(path)} is missing from the app folder.\n\n{COPYRIGHT_ASCII}"
