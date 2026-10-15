"""
Mud Music Manager - Start-up library check (v0.28.0, Checklist item 60i)

Running from source, the app runs on the PC's own Python. A new Python
version line (3.14 -> 3.15) is a separate install with none of the app's
libraries, and so is a fresh copy of the project, so the app used to stop
at its first import ("No module named 'colorama'") with no way forward but
typing `pip install -r requirements.txt`. Michael (2026-10-14): the Python
reinstall stays manual, but "automate everything else. So that if the user
did update python it is very easy to reacquire everything."

ensure_requirements() runs in main.py before any library is imported:

  1. missing(): every entry of requirements.txt is installed for THIS Python
     and at least the version it asks for (standard library only, a few ms).
  2. If something is missing it asks (a Windows message box; in a terminal,
     a y/n question): "install them now?"
  3. Yes: this file runs again as a script in its own console window (so the
     download's progress shows): pip is set up first if this Python has none
     (ensurepip), then `pip install -r requirements.txt`. Its output also goes
     to logs/library_install_log.txt in the data folder.
  4. Checked again; when everything is there the app carries on starting in
     this same process (nothing was imported yet). Otherwise a message says
     what is still missing and what to do (most often: a library has no build
     for a brand-new Python yet, so use the previous Python until it has).

Deno and FFmpeg don't depend on Python: the update scan downloads them into
the data folder when they're missing (component_updates). The installed /
portable app carries its own Python and libraries and skips all of this.

Standard library + app_paths only: it runs before any library exists.
"""

import importlib
import os
import re
import site
import subprocess
import sys
import time
from typing import List, Optional, Tuple

import app_paths

TITLE = "Mud Music Manager"
LOG_NAME = "library_install_log.txt"
_CREATE_NEW_CONSOLE = 0x00000010
_MB_YESNO_QUESTION = 0x00000004 | 0x00000020
_MB_ERROR = 0x00000010
_MB_INFO = 0x00000040
_IDYES = 6


# ---------------------------------------------------------------------------
# What is missing
# ---------------------------------------------------------------------------

def requirements_path() -> str:
    return app_paths.resource_path("requirements.txt")


def _requirements() -> List[Tuple[str, Optional[str]]]:
    """(name, minimum version or None) for each line of requirements.txt."""
    out = []
    try:
        with open(requirements_path(), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(.*)", line)
        if not m:
            continue
        minimum = re.search(r">=\s*([0-9][0-9A-Za-z.]*)", m.group(2))
        out.append((m.group(1), minimum.group(1) if minimum else None))
    return out


def _numbers(version: str) -> Tuple[int, ...]:
    """'2026.08.19' -> (2026, 8, 19); enough to compare with a minimum."""
    parts = []
    for piece in str(version).split("."):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group(0)))
    return tuple(parts)


def missing() -> List[str]:
    """requirements.txt entries not installed for this Python, or older than
    the version they ask for ("name" or "name (1.0 installed, needs 2.0)")."""
    from importlib import metadata
    out = []
    for name, minimum in _requirements():
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            out.append(name)
            continue
        if minimum and _numbers(have) < _numbers(minimum):
            out.append(f"{name} ({have} installed, needs {minimum})")
    return out


# ---------------------------------------------------------------------------
# Asking and telling
# ---------------------------------------------------------------------------

def _has_console() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty() and sys.stdout is not None
    except Exception:
        return False


def _ask(text: str) -> bool:
    if _has_console():
        print(f"\n{text}\n")
        try:
            return input("Install them now? (y/n): ").strip().lower().startswith("y")
        except (EOFError, KeyboardInterrupt):
            return False
    if os.name == "nt":
        import ctypes
        return ctypes.windll.user32.MessageBoxW(None, text + "\n\nInstall them now?", TITLE,
                                                _MB_YESNO_QUESTION) == _IDYES
    return False


def _tell(text: str, error: bool = True) -> None:
    if _has_console():
        print(f"\n{text}\n")
        return
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, TITLE, _MB_ERROR if error else _MB_INFO)
            return
        except Exception:
            pass
    sys.stderr.write(f"{TITLE}: {text}\n")


def _log_path() -> str:
    folder = app_paths.data_path("logs")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, LOG_NAME)


def _console_python() -> str:
    exe = sys.executable or "python"
    folder, name = os.path.split(exe)
    if name.lower() == "pythonw.exe" and os.path.exists(os.path.join(folder, "python.exe")):
        return os.path.join(folder, "python.exe")
    return exe


# ---------------------------------------------------------------------------
# The check main.py runs
# ---------------------------------------------------------------------------

def ensure_requirements(interactive: bool = True) -> bool:
    """True when every library is there (after installing them if the user
    agreed). False: the caller should stop (the user has been told why).
    interactive=False (the self-test, a scheduled sync): never asks; says
    what is missing and returns False. Never raises."""
    if app_paths.FROZEN:
        return True
    try:
        need = missing()
    except Exception:
        return True  # can't tell: let the imports speak for themselves
    if not need:
        return True
    version = ".".join(str(x) for x in sys.version_info[:3])
    listing = ", ".join(need[:12]) + (f" and {len(need) - 12} more" if len(need) > 12 else "")
    intro = (f"Mud Music Manager needs some libraries that aren't installed for this Python ({version}): "
             f"{listing}.")
    if len(need) >= len(_requirements()) - 1:
        intro += ("\n\nThis usually means Python was updated to a new version, or this is a new copy of the "
                  "app. Installing them downloads a few hundred MB and takes a few minutes.")
    if not interactive:
        _tell(intro + "\n\nStart the app normally to install them, or run:  pip install -r requirements.txt")
        return False
    if not _ask(intro):
        _tell("Nothing was installed, so Mud Music Manager can't start yet.\n\nStart it again to install them, or "
              f"run this in a terminal in the app's folder:\n\n  \"{_console_python()}\" -m pip install -r "
              "requirements.txt", error=False)
        return False
    code = _run_installer()
    importlib.invalidate_caches()
    user_site = site.getusersitepackages() if hasattr(site, "getusersitepackages") else None
    if user_site and os.path.isdir(user_site) and user_site not in sys.path:
        site.addsitedir(user_site)  # pip fell back to the user folder; it didn't exist at start-up
    still = missing()
    if not still:
        return True
    _tell(f"Some libraries still couldn't be installed for Python {version}: {', '.join(still)}.\n\n"
          f"pip's full output is in:\n{_log_path()}\n\n"
          "If Python was just updated to a new version, a library may not have a build for it yet. Your previous "
          "Python is still installed: open the app with it (for example  "
          f"py -{sys.version_info[0]}.{sys.version_info[1] - 1} main.py  in the app's folder) until the library "
          "catches up, then start it with the new one again."
          + ("" if code == 0 else f"\n\n(The installer ended with code {code}.)"))
    return False


def _run_installer() -> int:
    """This file as a script in its own console window (shows pip's
    progress, also written to the log). Returns its exit code."""
    cmd = [_console_python(), os.path.abspath(__file__), "--install", requirements_path(), _log_path()]
    try:
        if _has_console():
            return subprocess.call(cmd)
        flags = _CREATE_NEW_CONSOLE if os.name == "nt" else 0
        return subprocess.call(cmd, creationflags=flags)
    except OSError as e:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(f"Couldn't start the installer: {e}\n")
        return 1


# ---------------------------------------------------------------------------
# The installer (this file run as a script)
# ---------------------------------------------------------------------------

def _install_main(requirements: str, log_path: str) -> int:
    with open(log_path, "a", encoding="utf-8") as log:
        def say(text: str) -> None:
            print(text, flush=True)
            log.write(text + "\n")
            log.flush()

        def run(args: List[str]) -> Tuple[int, str]:
            say("> " + " ".join(args))
            p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace")
            text = []
            for line in p.stdout:
                line = line.rstrip()
                text.append(line)
                say(line)
            return p.wait(), "\n".join(text)

        say(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}  Mud Music Manager: installing its libraries for "
            f"Python {sys.version.split()[0]} ({sys.executable}) ===")
        py = sys.executable
        if subprocess.call([py, "-m", "pip", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
            say("pip isn't set up for this Python yet; setting it up first...")
            code, _out = run([py, "-m", "ensurepip", "--upgrade"])
            if code:
                say("Couldn't set pip up.")
                return _finish(say, code)
        base = [py, "-m", "pip", "install", "--disable-pip-version-check"]
        extra = []
        code, out = run(base + ["-r", requirements])
        if code and "externally-managed-environment" in out:
            extra = ["--break-system-packages"]
            code, out = run(base + ["-r", requirements] + extra)
        if code:
            # pip installs all or nothing: one library with no build for this Python
            # would stop every other one. Install the rest one at a time.
            say("\nNot everything could be installed together; trying the libraries one at a time...")
            failed = []
            for line in _requirement_lines(requirements):
                one, _out = run(base + [line] + extra)
                if one:
                    failed.append(line)
            code = 1 if failed else 0
            if failed:
                say("\nCouldn't install: " + ", ".join(failed))
        return _finish(say, code)


def _requirement_lines(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.split("#", 1)[0].strip() for ln in f.read().splitlines()]
    except OSError:
        return []
    return [ln for ln in lines if ln and not ln.startswith("-")]


def _finish(say, code: int) -> int:
    if code == 0:
        say("\nDone. Mud Music Manager is starting...")
        time.sleep(2)
    else:
        say("\nSomething went wrong (see above). Press Enter to close this window.")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--install":
    sys.exit(_install_main(sys.argv[2], sys.argv[3]))
