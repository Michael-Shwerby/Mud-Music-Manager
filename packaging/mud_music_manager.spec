# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build of Mud Music Manager (v0.19.1, Checklist item 60c;
v0.20.0: version details, licence files, unused Qt parts left out).

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
import re

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "assets", "app_icon.ico")

# The version and owner come from app_info.py (read as text, not imported).
with open(os.path.join(ROOT, "app_info.py"), encoding="utf-8") as _f:
    _info = _f.read()
VERSION = re.search(r'^VERSION = "([0-9.]+)"', _info, re.M).group(1)
_parts = tuple(int(p) for p in (VERSION.split(".") + ["0", "0", "0"])[:4])


def version_info(description: str, exe_name: str) -> VSVersionInfo:
    """Windows version details (Properties -> Details) for one program (v0.20.0)."""
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=_parts, prodvers=_parts),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "Michael Xuereb"),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", exe_name),
                StringStruct("LegalCopyright", "Copyright (c) 2026 Michael Xuereb. GNU GPL v3 or later."),
                StringStruct("OriginalFilename", exe_name + ".exe"),
                StringStruct("ProductName", "Mud Music Manager"),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

datas = [
    (os.path.join(ROOT, "assets"), "assets"),
    (os.path.join(ROOT, "LICENSE.txt"), "."),
    (os.path.join(ROOT, "THIRD_PARTY_NOTICES.txt"), "."),
    (os.path.join(ROOT, "requirements.txt"), "."),  # Settings -> Project libraries lists these
]
datas += collect_data_files("ytmusicapi")   # its locale files
datas += collect_data_files("yt_dlp_ejs")   # the JavaScript yt-dlp runs in Deno for YouTube's checks
# Package metadata: Settings -> Project libraries shows each library's version
# (importlib.metadata), and some libraries read their own. Each *.dist-info
# folder also carries the library's licence text (THIRD_PARTY_NOTICES.txt
# points to them), so the libraries they bring in are listed too (v0.20.0).
for dist in ("spotipyFree", "spotapi", "ytmusicapi", "yt-dlp", "yt-dlp-ejs", "mutagen", "requests",
             "syncedlyrics", "packaging", "colorama", "PySide6", "PySide6-Essentials", "PySide6-Addons",
             "shiboken6", "redis", "websockets", "curl_cffi", "pymongo", "rapidfuzz", "beautifulsoup4",
             "pillow", "pyotp", "readerwriterlock", "validators", "certifi", "urllib3", "charset-normalizer",
             "idna", "typing_extensions"):
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
# v0.20.0: Qt files the app never uses, left out to make the download smaller:
# Qt's software OpenGL fallback (the app draws with plain widgets, ~20 MB) and
# Qt's PDF reader with its image plugin.
DROP = {"opengl32sw.dll", "qt6pdf.dll", "qpdf.dll"}
a.binaries = [b for b in a.binaries if os.path.basename(b[0]).lower() not in DROP]

pyz = PYZ(a.pure)

gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Mud Music Manager",
    icon=ICON,
    console=False,
    version=version_info("Mud Music Manager", "Mud Music Manager"),
)
cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Mud Music Manager CLI",
    icon=ICON,
    console=True,
    version=version_info("Mud Music Manager (terminal menu)", "Mud Music Manager CLI"),
)
coll = COLLECT(
    gui, cli, a.binaries, a.datas,
    name="Mud Music Manager",
)
