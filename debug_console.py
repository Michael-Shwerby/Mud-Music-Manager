"""
Mud Music Manager - Debug Console support (v0.16.7)

Everything about the "hidden console + Debug Window" behaviour (Checklist
item 10) that does not need Qt lives here, so main.py can use it before the
heavy imports and the GUI can share it. Standard library only.

What it does
------------
1. Launch without a console (Windows). Double-clicking main.py runs it with
   python.exe, which always opens a console window (and closing that window
   kills the app). When the GUI is about to start and that console belongs to
   this launch alone, relaunch_without_console_if_owned() starts the same
   script again under pythonw.exe (no console) and the first process exits.
   If anything about that looks wrong (started from a terminal you opened,
   no pythonw.exe, PySide6 missing...) it does nothing and the app keeps its
   console exactly as before.

2. Capture the output. install_capture() wraps sys.stdout / sys.stderr so
   everything printed is also kept in a ring buffer (the last 3000 lines),
   and logger.py / the background tasks add their lines to it too. Under
   pythonw sys.stdout is None; the wrapper makes print() safe there.

3. The Debug Window. open_external_console() (Windows) starts a small helper
   process in its own real console and streams the buffer + live lines to it
   through a pipe on a background thread. The app never waits on it, and
   closing that console (its X button) only ends the helper - the app is a
   different process and is unaffected. Settings -> "Close Debug Window"
   closes it from inside the app. On other systems ui_debug.py shows an
   in-app log window fed from the same buffer.
"""

import collections
import io
import os
import queue
import re
import subprocess
import sys
import threading
from typing import Callable, List, Optional

IS_WINDOWS = sys.platform.startswith("win")

RELAUNCH_ENV = "MMM_GUI_RELAUNCHED"   # set on the relaunched process (guards against loops)
CLI_NOTE_ENV = "MMM_CLI_NOTE"          # a one-line note the terminal menu prints first
CLI_EMBEDDED_ENV = "MMM_CLI_EMBEDDED"  # set by the in-app CMD window: the menu talks over pipes (v0.16.7)
# v0.17.9: set by both CMD button modes (in-app window, OS terminal): the
# graphical app is running beside this menu (its Settings -> Import is refused).
CLI_FROM_GUI_ENV = "MMM_CLI_FROM_GUI"

_CREATE_NEW_CONSOLE = 0x00000010
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000

MAX_LINES = 3000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_lines: "collections.deque[str]" = collections.deque(maxlen=MAX_LINES)
_listeners: List[Callable[[str], None]] = []
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# The line buffer
# ---------------------------------------------------------------------------

def write_line(text) -> None:
    """Add text (one or more lines) to the buffer and tell the listeners.
    Safe from any thread; never raises."""
    try:
        parts = str(text).splitlines() or [""]
    except Exception:
        return
    with _lock:
        for p in parts:
            _lines.append(p)
        listeners = list(_listeners)
    for fn in listeners:
        for p in parts:
            try:
                fn(p)
            except Exception:
                pass


def recent_lines() -> List[str]:
    with _lock:
        return list(_lines)


def add_listener(fn: Callable[[str], None]) -> List[str]:
    """Register fn(line) for every future line. Returns the buffered lines as
    they were at that instant, so a viewer can replay them without gaps or
    duplicates."""
    with _lock:
        _listeners.append(fn)
        return list(_lines)


def remove_listener(fn: Callable[[str], None]) -> None:
    with _lock:
        try:
            _listeners.remove(fn)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Capturing print() / stderr
# ---------------------------------------------------------------------------

class _Capture:
    """File-like tee: forwards to the real stream (if there is one) and
    records completed lines in the buffer."""

    def __init__(self, original, prefix: str = ""):
        self._orig = original
        self._prefix = prefix
        self._partial = ""
        self._plock = threading.Lock()
        self.encoding = getattr(original, "encoding", None) or "utf-8"
        self.errors = getattr(original, "errors", None) or "replace"

    # -- file API ------------------------------------------------------
    def write(self, s):
        if not isinstance(s, str):
            try:
                s = bytes(s).decode("utf-8", "replace")
            except Exception:
                s = str(s)
        if self._orig is not None:
            try:
                self._orig.write(s)
            except Exception:
                pass
        try:
            self._feed(s)
        except Exception:
            pass
        return len(s)

    def _feed(self, s: str) -> None:
        with self._plock:
            data = self._partial + s
            *done, self._partial = data.split("\n")
            if len(self._partial) > 8192:  # a runaway line with no newline
                done.append(self._partial)
                self._partial = ""
        for line in done:
            line = _ANSI.sub("", line.rstrip("\r"))
            if "\r" in line:               # progress bars redraw with \r
                line = line.split("\r")[-1]
            if line.strip():
                write_line(self._prefix + line)

    def flush(self):
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    def fileno(self):
        raise io.UnsupportedOperation("fileno")

    def __getattr__(self, name):
        if self._orig is not None and not name.startswith("__"):
            return getattr(self._orig, name)
        raise AttributeError(name)


_capture_installed = False


def install_capture() -> None:
    """Route everything printed (stdout) and written to stderr into the
    buffer, in addition to wherever it already went. Idempotent."""
    global _capture_installed
    if _capture_installed:
        return
    _capture_installed = True
    sys.stdout = _Capture(sys.stdout)
    sys.stderr = _Capture(sys.stderr, "[err] ")


# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------

def has_console() -> bool:
    """True if this process has a console window/terminal attached.
    Always True off Windows (a terminal is assumed)."""
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return True


def cli_can_run_here() -> bool:
    """True if the terminal menu (`--cli`) can run in this process as it is.
    It can with a console, or when the in-app CMD window started it (v0.16.7):
    that window feeds it through pipes and Qt starts it with no console window
    at all, so has_console() alone would wrongly say no and a separate OS
    terminal would be opened instead."""
    if has_console():
        return True
    return bool(os.environ.get(CLI_EMBEDDED_ENV)) and sys.stdin is not None and sys.stdout is not None


def console_python_executable() -> str:
    """The interpreter to use for anything that needs a console or captured
    pipes. Under pythonw.exe that is its python.exe sibling."""
    exe = sys.executable or "python"
    if IS_WINDOWS:
        folder, name = os.path.split(exe)
        if name.lower() == "pythonw.exe":
            candidate = os.path.join(folder, "python.exe")
            if os.path.exists(candidate):
                return candidate
    return exe


def hidden_window_flags() -> int:
    """creationflags that stop a child process from flashing a console
    window (0 off Windows). Used for pip when running without a console."""
    return _CREATE_NO_WINDOW if IS_WINDOWS else 0


_LAUNCHER_NAMES = {"py.exe", "pyw.exe", "python.exe", "pythonw.exe"}


def _console_is_ours() -> bool:
    """True if every process attached to this console is just Python (or the
    py.exe launcher) - i.e. the console exists only because this launch
    created it, as with a double-click. False if a shell (cmd, PowerShell,
    an IDE terminal...) is also attached, or if we can't tell."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pids = (wintypes.DWORD * 16)()
    count = kernel32.GetConsoleProcessList(pids, 16)
    if count < 1 or count > 16:
        return False
    attached = {int(pids[i]) for i in range(count)}
    attached.discard(os.getpid())
    if not attached:
        return True

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return False
    names = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names[int(entry.th32ProcessID)] = entry.szExeFile.lower()
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return all(names.get(pid) in _LAUNCHER_NAMES for pid in attached)


def relaunch_without_console_if_owned(script: str, args: List[str]) -> bool:
    """Windows only. If this process was started by double-clicking (it owns
    its console), start `script` again under pythonw.exe with no console and
    return True - the caller should then exit immediately. Returns False,
    changing nothing, in every other case."""
    if not IS_WINDOWS or os.environ.get(RELAUNCH_ENV):
        return False
    try:
        if not has_console():
            return False
        exe_folder, exe_name = os.path.split(sys.executable or "")
        if exe_name.lower() == "pythonw.exe":
            return False
        pythonw = os.path.join(exe_folder, "pythonw.exe")
        if not os.path.exists(pythonw):
            return False
        import importlib.util
        if importlib.util.find_spec("PySide6") is None:
            return False  # the GUI can't start anyway; keep the console for the terminal menu
        if not _console_is_ours():
            return False
        env = os.environ.copy()
        env[RELAUNCH_ENV] = "1"
        subprocess.Popen(
            [pythonw, script] + list(args),
            env=env, cwd=os.getcwd(), close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        )
        return True
    except Exception:
        return False


def show_message_box(title: str, text: str) -> None:
    """A plain OS message box (Windows) so a failure is visible even when
    there is no console. Elsewhere it just writes to stderr. Never raises."""
    try:
        if IS_WINDOWS:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x10)
        else:
            sys.stderr.write(f"{title}: {text}\n")
    except Exception:
        pass


def install_early_excepthook() -> None:
    """Until main.py installs its fuller crash diagnostics: any uncaught
    exception is appended to logs/error_log.txt and, if there is no console to
    show it in, put in a message box."""
    def hook(exc_type, exc, tb):
        try:
            import traceback
            from datetime import datetime
            text = "".join(traceback.format_exception(exc_type, exc, tb)).strip()
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "error_log.txt"), "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ERROR: Uncaught exception (startup):\n{text}\n")
            if not has_console():
                show_message_box("Mud Music Manager", f"The app hit a problem while starting:\n\n{text[-1500:]}")
        except Exception:
            pass
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass
    sys.excepthook = hook


def launch_cli_in_new_console(script: str, note: str = "") -> Optional[str]:
    """Open the terminal menu (`script --cli`) in a brand-new console window.
    Used when the app has no console of its own but needs one (the GUI can't
    start, or --cli was started without a terminal). Returns None on
    success, otherwise a short explanation."""
    if not IS_WINDOWS:
        return "No terminal is attached."
    try:
        env = os.environ.copy()
        if note:
            env[CLI_NOTE_ENV] = note
        subprocess.Popen(
            [console_python_executable(), script, "--cli"],
            env=env, cwd=os.path.dirname(os.path.abspath(script)),
            creationflags=_CREATE_NEW_CONSOLE,
        )
        return None
    except Exception as e:
        return f"Couldn't open a console window: {e}"


# ---------------------------------------------------------------------------
# The external (real console) Debug Window
# ---------------------------------------------------------------------------

# Runs inside the helper process, in its own console. Reads UTF-8 lines from
# the pipe and prints them; ends when the pipe closes.
HELPER_SCRIPT = r"""
import os, sys
# The app runs under pythonw.exe, which has no standard output handles for this
# helper to inherit - so write to the helper's own console directly (v0.16.4).
if os.name == "nt":
    try:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = sys.stdout
    except Exception:
        pass
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    import ctypes
    ctypes.windll.kernel32.SetConsoleTitleW("Mud Music Manager - Debug Window")
except Exception:
    pass
print("Mud Music Manager - Debug Window")
print("Live log + printed output from the app. Closing this window does not close the app.")
print("-" * 70)
sys.stdout.flush()
for line in sys.stdin:
    sys.stdout.write(line)
    sys.stdout.flush()
"""

_ext_proc: Optional[subprocess.Popen] = None
_ext_queue: "Optional[queue.Queue]" = None
_ext_lock = threading.Lock()


def external_console_open() -> bool:
    with _ext_lock:
        return _ext_proc is not None and _ext_proc.poll() is None


def _ext_feed(line: str) -> None:
    q = _ext_queue
    if q is not None:
        try:
            q.put_nowait(line)
        except queue.Full:
            pass  # never block the app because the console can't keep up


def _ext_writer(proc: subprocess.Popen, q: "queue.Queue") -> None:
    try:
        while True:
            line = q.get()
            if line is None:
                break
            proc.stdin.write((line + "\n").encode("utf-8", "replace"))
            proc.stdin.flush()
    except Exception:
        pass  # console closed (or the pipe broke): just stop
    finally:
        remove_listener(_ext_feed)
        try:
            proc.stdin.close()
        except Exception:
            pass


def open_external_console() -> Optional[str]:
    """Windows: open a real console window showing the buffer so far and then
    every new line. Returns None on success (or if already open), otherwise a
    short explanation."""
    global _ext_proc, _ext_queue
    if not IS_WINDOWS:
        return "A separate console window is only available on Windows."
    with _ext_lock:
        if _ext_proc is not None and _ext_proc.poll() is None:
            return None
        try:
            proc = subprocess.Popen(
                [console_python_executable(), "-u", "-c", HELPER_SCRIPT],
                stdin=subprocess.PIPE, creationflags=_CREATE_NEW_CONSOLE, close_fds=True,
            )
        except Exception as e:
            return f"Couldn't open the Debug Window: {e}"
        q: "queue.Queue" = queue.Queue(maxsize=MAX_LINES * 2)
        _ext_proc, _ext_queue = proc, q
    for line in add_listener(_ext_feed):  # replay what happened before it was opened
        _ext_feed(line)
    threading.Thread(target=_ext_writer, args=(proc, q), name="mmm-debug-console", daemon=True).start()
    return None


def close_external_console() -> None:
    global _ext_proc, _ext_queue
    with _ext_lock:
        proc, q = _ext_proc, _ext_queue
        _ext_proc, _ext_queue = None, None
    remove_listener(_ext_feed)
    if q is not None:
        try:
            q.put_nowait(None)
        except queue.Full:
            pass
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
