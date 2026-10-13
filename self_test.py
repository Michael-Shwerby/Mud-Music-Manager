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
    "app_info", "app_paths", "app_update", "logger", "debug_console", "config_manager", "exceptions_manager",
    "track_failures", "ui_settings", "user_data", "download_settings", "youtube_cookies", "downloader",
    "metadata_tagger", "lyrics_provider", "youtube_matcher", "spotify_reader", "youtube_playlist_reader",
    "ytmusic_reader", "soundcloud_reader", "apple_music_reader", "amazon_music_reader", "acquisition_engine",
    "library_sync", "artist_watch", "dependency_checker", "ui_libraries",
)
GUI_MODULES = (
    "ui_theme", "ui_widgets", "ui_keys", "ui_dialogs", "ui_backend", "ui_main_page", "ui_edit_page",
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
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no window is shown
    for name in GUI_MODULES:
        r.attempt(name, lambda n=name: importlib.import_module(n) and None)

    print("\nQt")
    r.attempt("QtWidgets / QtGui", lambda: importlib.import_module("PySide6.QtWidgets") and None)
    r.attempt("QtMultimedia (Edit page player)", lambda: importlib.import_module("PySide6.QtMultimedia") and None)
    r.check("app icon file", os.path.isfile(app_info.ICON_ICO_PATH), app_info.ICON_ICO_PATH)
    r.check("licence texts", os.path.isfile(app_info.LICENSE_PATH) and os.path.isfile(app_info.THIRD_PARTY_PATH))

    print("\nTools")
    import downloader
    ffmpeg = downloader.ffmpeg_path()
    r.check("FFmpeg", bool(ffmpeg), ffmpeg or "not found -- conversions and some downloads will fail")
    deno = shutil.which("deno")
    r.check("Deno", bool(deno), deno or "not found -- many YouTube downloads will fail")
    r.attempt("yt-dlp", lambda: f"v{importlib.import_module('yt_dlp.version').__version__}")
    r.attempt("yt-dlp-ejs (YouTube challenge scripts)", lambda: importlib.import_module("yt_dlp_ejs") and None)

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
