# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build of Mud Music Manager (v0.19.1, Checklist item 60c).

Run through packaging/build.ps1 (which makes a clean build environment).
Produces dist/Mud Music Manager/, a "one-folder" build holding:

  Mud Music Manager.exe      the app (windowed: no console)
  Mud Music Manager CLI.exe  the same program with a console, for the
                             terminal menu (--cli), the Debug Window helper
                             (--debug-console) and the self-test (--selftest)
  _internal/                 Python, every library, assets and licence texts,
                             shared by both programs
  tools/                     FFmpeg and Deno (copied in by build.ps1)

The project's modules are found from main.py's imports; the ones only
imported by name (the self-test, the GUI started inside main()) are listed
in HIDDEN. Libraries that read their own data files or package metadata at
run time get them collected below.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "assets", "app_icon.ico")

datas = [
    (os.path.join(ROOT, "assets"), "assets"),
    (os.path.join(ROOT, "LICENSE.txt"), "."),
    (os.path.join(ROOT, "THIRD_PARTY_NOTICES.txt"), "."),
    (os.path.join(ROOT, "requirements.txt"), "."),  # Settings -> Project libraries lists these
]
datas += collect_data_files("ytmusicapi")   # its locale files
datas += collect_data_files("yt_dlp_ejs")   # the JavaScript yt-dlp runs in Deno for YouTube's checks
# Package metadata: Settings -> Project libraries shows each library's version
# (importlib.metadata), and some libraries read their own.
for dist in ("spotipyFree", "spotapi", "ytmusicapi", "yt-dlp", "yt-dlp-ejs", "mutagen", "requests",
             "syncedlyrics", "packaging", "colorama", "PySide6"):
    try:
        datas += copy_metadata(dist)
    except Exception as e:  # a library missing from the build environment
        print(f"WARNING: no metadata for {dist}: {e}")

HIDDEN = [
    "ui_app", "self_test", "ui_debug",
    *collect_submodules("yt_dlp_ejs"),
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=HIDDEN,
    excludes=["pytest", "IPython", "matplotlib", "numpy.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Mud Music Manager",
    icon=ICON,
    console=False,
)
cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Mud Music Manager CLI",
    icon=ICON,
    console=True,
)
coll = COLLECT(
    gui, cli, a.binaries, a.datas,
    name="Mud Music Manager",
)
