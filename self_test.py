"""
Mud Music Manager - Build self-test (v0.19.1, Checklist item 60c)

`Mud Music Manager CLI.exe --selftest` (or `python main.py --selftest`)
checks that this copy of the app has everything it needs, so a packaged
build can be tried on a clean computer (one with no Python, FFmpeg or Deno
installed) before it is released:

  - which program and folders it runs from (packaged or source, data folder,
    tools folder);
  - every project module and the libraries they need import;
  - the Qt parts the window and the Edit page player use load;
  - FFmpeg and Deno are found (Deno lets yt-dlp answer YouTube's JavaScript
    checks), and the yt-dlp-ejs scripts it uses are there;
  - unless --no-download is given: one real download of a short public video
    (YouTube's first ever video, 19 seconds) into a temporary folder, through
    the app's own downloader (the download settings in use apply), checking
    the audio file comes out. The temporary folder is deleted afterwards.

Prints one line per check and returns 0 if every check passed, else 1.
Reads the data folder but never writes to it, and touches no playlist or
music folder.
"""

import importlib
import os
import shutil
import sys
import tempfile
import traceback

TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo", 19 s

PROJECT_MODULES = (
    "app_info", "app_paths", "app_update", "component_updates", "logger", "debug_console", "config_manager", "exceptions_manager",
    "track_failures", "ui_settings", "user_data", "download_settings", "youtube_cookies", "downloader",
    "metadata_tagger", "lyrics_provider", "youtube_matcher", "spotify_reader", "youtube_playlist_reader",
    "ytmusic_reader", "soundcloud_reader", "apple_music_reader", "amazon_music_reader", "acquisition_engine",
    "library_sync", "playlist_file", "scheduled_sync", "device_copy", "artist_watch", "dependency_checker", "ui_libraries",
    "update_scan", "bootstrap",  # v0.28.0 (Checklist items 60h, 60i)
)
# Libraries a project module imports only when first used (v0.23.3).
LAZY_LIBRARIES = ("spotapi", "spotapi.http.request", "SpotipyFree", "SpotipyFree.Formatter", "syncedlyrics")
GUI_MODULES = (
    "ui_theme", "ui_widgets", "ui_keys", "ui_dialogs", "ui_backend",
    # v0.23.4: the Main Menu and Edit page are split over several modules
    "ui_main_rows", "ui_sync_panel", "ui_main_runs", "ui_main_keys", "ui_main_page",
    "ui_edit_rows", "ui_edit_player", "ui_edit_keys", "ui_edit_page",
    "ui_settings_page", "ui_results_page", "ui_remap", "ui_debug", "ui_cmd", "ui_app",
)


class _Run:
    def __init__(self):
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if not ok:
            self.failed += 1
        print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
        return ok

    def attempt(self, name: str, fn) -> bool:
        try:
            detail = fn()
            return self.check(name, True, detail or "")
        except Exception as e:
            return self.check(name, False, f"{type(e).__name__}: {e}")


def _library_count(libs: dict) -> str:
    missing = sorted(lib["name"] for lib in libs.values() if not lib.get("version"))
    if missing:
        raise RuntimeError("not found: " + ", ".join(missing))
    return f"{len(libs)} libraries"


def run(download: bool = True) -> int:
    import app_info
    import app_paths

    r = _Run()
    print(f"Mud Music Manager v{app_info.VERSION} self-test")
    print(f"  program: {'packaged' if app_paths.FROZEN else 'source'} -- {sys.executable}")
    print(f"  program folder: {app_paths.APP_DIR}")
    print(f"  data folder:    {app_paths.DATA_DIR}")
    print(f"  tools folder:   {app_paths.TOOLS_DIR if os.path.isdir(app_paths.TOOLS_DIR) else '(none)'}")
    print()

    print("Modules")
    for name in PROJECT_MODULES:
        r.attempt(name, lambda n=name: importlib.import_module(n) and None)
    # v0.23.3: spotify_reader imports these on first use (faster launch), so
    # importing it no longer proves they are in the build; check them here.
    for name in LAZY_LIBRARIES:
        r.attempt(name, lambda n=name: importlib.import_module(n) and None)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no window is shown
    for name in GUI_MODULES:
        r.attempt(name, lambda n=name: importlib.import_module(n) and None)

    print("\nQt")
    r.attempt("QtWidgets / QtGui", lambda: importlib.import_module("PySide6.QtWidgets") and None)
    r.attempt("QtMultimedia (Edit page player)", lambda: importlib.import_module("PySide6.QtMultimedia") and None)
    r.check("app icon file", os.path.isfile(app_info.ICON_ICO_PATH), app_info.ICON_ICO_PATH)
    r.check("Ko-fi logo file", os.path.isfile(app_info.KOFI_ICON_PATH), app_info.KOFI_ICON_PATH)  # v0.27.0
    r.check("licence texts", os.path.isfile(app_info.LICENSE_PATH) and os.path.isfile(app_info.THIRD_PARTY_PATH))

    print("\nTools")
    import downloader
    ffmpeg = downloader.ffmpeg_path()
    r.check("FFmpeg", bool(ffmpeg), ffmpeg or "not found -- conversions and some downloads will fail")
    deno = shutil.which("deno")
    r.check("Deno", bool(deno), deno or "not found -- many YouTube downloads will fail")
    r.attempt("yt-dlp", lambda: f"v{importlib.import_module('yt_dlp.version').__version__} "
                                f"from {os.path.dirname(os.path.dirname(importlib.import_module('yt_dlp').__file__))}")
    r.attempt("yt-dlp-ejs (YouTube challenge scripts)", lambda: importlib.import_module("yt_dlp_ejs") and None)
    # v0.22.0 (Checklist item 60g): the versions in use, built in or updated
    import component_updates
    r.attempt("component versions", lambda: ", ".join(f"{k} {v}" for k, v in sorted(
        component_updates.current_versions().items())))
    # v0.28.0 (Checklist item 60h): the list of libraries the update scan checks
    import dependency_checker
    r.attempt("libraries the update check covers", lambda: _library_count(dependency_checker.installed_libraries()))

    if download:
        print("\nDownload")
        tmp = tempfile.mkdtemp(prefix="mmm_selftest_")
        try:
            path, reason, kind = downloader.download_audio(TEST_VIDEO, tmp, "Me at the zoo")
            if r.check("download a 19-second test video", path is not None, reason or ""):
                size = os.path.getsize(path)
                r.check("audio file written", size > 10_000, f"{os.path.basename(str(path))}, {size:,} bytes")
        except Exception:
            r.check("download a 19-second test video", False, traceback.format_exc(limit=3).strip())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("All checks passed." if not r.failed else f"{r.failed} check(s) failed.")
    return 0 if not r.failed else 1
