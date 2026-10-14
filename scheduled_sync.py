"""
Mud Music Manager - Scheduled sync (v0.25.0, Checklist item 65)

Syncs every playlist once a day at a time the user picks, without opening
the app. Michael's choices (2026-10-13): optional and **Off by default**;
a Windows scheduled task runs it even when the app is closed; when the app
is open at that moment, the open app runs it itself, so two copies never
write at once; the time is configurable; a notification when it finishes,
**On by default**.

How it fits together:

  - The setting (ui_settings "scheduled_sync": enabled, time "HH:MM",
    notify) is changed in Settings or the terminal menu; apply() then
    creates, changes or removes the task "\\Mud Music Manager\\Scheduled
    Sync" with schtasks (per user, no administrator prompt). The task is
    imported from XML so that a run missed while the PC was off or asleep
    starts when it is next on (StartWhenAvailable), it runs on battery,
    only with a network, never twice at once, and for at most 12 hours.
    It runs only while the user is signed in (so it can show its
    notification). The app checks the task each time it opens (apply()
    again), so a moved or updated program keeps a working task.

  - The task starts the windowed program with --scheduled-sync (from
    source: pythonw.exe main.py --scheduled-sync), which calls
    run_headless(). No window opens.

  - run_headless(): if the app is open (app_is_open(): its presence file
    names a live process with a recent heartbeat), it leaves a request
    file and waits up to 60 s for the app to take it; the app's timer
    (every 5 s) takes it and runs a normal Sync Now of every playlist, as
    if each were ticked. Otherwise it syncs every playlist itself, in
    playlist order, with the watched artists' new releases added first
    (as the terminal menu's Sync does), through library_sync.sync_playlist(),
    the same code the app uses. Each playlist's cached numbers for the
    Main Menu are updated as the app would (when Qt is available).

  - While a background run is going, its state file names its process.
    The app, opened meanwhile, asks first: wait for it (the window it
    shows follows its progress), or stop it at its next safe point (the
    download in progress finishes, as Cancel does), and only then opens.
    The terminal menu refuses to Sync while one runs. So there is never
    a second writer.

  - When it finishes, the result is stored (ui_settings
    "scheduled_sync_last", shown in Settings) and, if notify is on, a
    Windows notification says what happened ("3 new songs · 1 needs
    attention"). Clicking it opens the app.

Windows only (supported()); elsewhere the setting is shown as unavailable.
No Qt at import time: the terminal menu uses this module too; Qt is
imported only to show the notification.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from typing import Callable, List, Optional, Tuple

import app_info
import app_paths
import logger
import ui_settings

ARG = "--scheduled-sync"
SETTING_KEY = "scheduled_sync"
LAST_KEY = "scheduled_sync_last"
DEFAULTS = {"enabled": False, "time": "03:00", "notify": True}
TASK_NAME = "\\Mud Music Manager\\Scheduled Sync"

APP_PRESENCE_FILE = app_paths.data_path("app_running.json")      # the open app: pid, heartbeat
STATE_FILE = app_paths.data_path("scheduled_sync_state.json")    # a background run: pid, progress, stop
REQUEST_FILE = app_paths.data_path("scheduled_sync_request.json")  # background run -> open app

HEARTBEAT_SECONDS = 5
APP_STALE_SECONDS = 60          # the app's heartbeat older than this: it isn't running (crashed)
REQUEST_WAIT_SECONDS = 60       # how long a background run waits for the open app to take over
NOTIFY_SHOW_SECONDS = 10

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# library_sync's line just before a song's download: the safe point to stop at
_DOWNLOAD_START_RE = re.compile(r"^\[\d+/\d+\]\s+Downloading '")


def supported() -> bool:
    return sys.platform == "win32"


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------

def get_setting() -> dict:
    stored = ui_settings.get(SETTING_KEY)
    out = dict(DEFAULTS)
    if isinstance(stored, dict):
        if isinstance(stored.get("enabled"), bool):
            out["enabled"] = stored["enabled"]
        if isinstance(stored.get("time"), str) and _TIME_RE.match(stored["time"]):
            out["time"] = stored["time"]
        if isinstance(stored.get("notify"), bool):
            out["notify"] = stored["notify"]
    return out


def set_setting(**changes) -> dict:
    s = get_setting()
    for k, v in changes.items():
        if k not in DEFAULTS:
            raise KeyError(k)
        if k == "time" and not (isinstance(v, str) and _TIME_RE.match(v)):
            raise ValueError(f"not a time: {v!r}")
        s[k] = v
    ui_settings.set_value(SETTING_KEY, s)
    return s


def parse_time(text: str) -> Optional[str]:
    """'3', '3:5', '03:05', '15.30' -> 'HH:MM', or None."""
    m = re.match(r"^\s*(\d{1,2})(?:[:.](\d{1,2}))?\s*$", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def describe(s: Optional[dict] = None) -> str:
    s = s or get_setting()
    if not s["enabled"]:
        return "Off"
    return f"every day at {s['time']}" + ("" if s["notify"] else " (no notification)")


def last_result() -> Optional[dict]:
    v = ui_settings.get(LAST_KEY)
    return v if isinstance(v, dict) else None


def last_result_text() -> str:
    last = last_result()
    if not last:
        return "It hasn't run yet."
    try:
        when = datetime.fromtimestamp(float(last.get("at", 0))).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        when = "an unknown time"
    where = " (in the open app)" if last.get("in_app") else ""
    return f"Last run {when}{where}: {last.get('summary', '')}"


def record_result(summary: str, ok: bool, in_app: bool) -> None:
    ui_settings.set_value(LAST_KEY, {"at": time.time(), "summary": summary, "ok": ok, "in_app": in_app})
    logger.log_event(f"Scheduled sync{' (in the open app)' if in_app else ''}: {summary}")


# ---------------------------------------------------------------------------
# The Windows scheduled task
# ---------------------------------------------------------------------------

def task_command() -> Tuple[str, str]:
    """(program, arguments) the task runs: the windowed program, so no
    console window opens."""
    if app_paths.FROZEN:
        return os.path.join(app_paths.APP_DIR, "Mud Music Manager.exe"), ARG
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    main_py = os.path.join(app_paths.APP_DIR, "main.py")
    return (pyw if os.path.exists(pyw) else exe), f'"{main_py}" {ARG}'


def app_command() -> List[str]:
    """How to open the app (a notification click)."""
    program, _ = task_command()
    if app_paths.FROZEN:
        return [program]
    return [program, os.path.join(app_paths.APP_DIR, "main.py")]


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def task_xml(at: str) -> str:
    program, args = task_command()
    start = f"{datetime.now():%Y-%m-%d}T{at}:00"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>{_xml_escape(app_info.APP_NAME)}</Author>
    <Description>Syncs your playlists every day (Mud Music Manager → Settings → Scheduled sync).</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(program)}</Command>
      <Arguments>{_xml_escape(args)}</Arguments>
      <WorkingDirectory>{_xml_escape(app_paths.APP_DIR)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> Tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(["schtasks", *args], capture_output=True, text=True, timeout=30, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def task_exists() -> bool:
    if not supported():
        return False
    code, _ = _schtasks("/Query", "/TN", TASK_NAME)
    return code == 0


def install_task(at: str) -> Optional[str]:
    """Create or replace the task; None, or why it couldn't be done."""
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="mmm-task-")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(task_xml(at))
        code, out = _schtasks("/Create", "/TN", TASK_NAME, "/XML", path, "/F")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if code != 0:
        logger.log_error(f"Couldn't create the scheduled sync task: {out}")
        return out or "schtasks failed"
    logger.log_event(f"Scheduled sync task set: every day at {at} ({' '.join(task_command())}).")
    return None


def remove_task() -> Optional[str]:
    if not task_exists():
        return None
    code, out = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if code != 0:
        logger.log_error(f"Couldn't remove the scheduled sync task: {out}")
        return out or "schtasks failed"
    logger.log_event("Scheduled sync task removed.")
    return None


def apply() -> Optional[str]:
    """Make the task match the setting. None, or why it couldn't."""
    if not supported():
        return "Scheduled sync is only available on Windows."
    s = get_setting()
    return install_task(s["time"]) if s["enabled"] else remove_task()


# ---------------------------------------------------------------------------
# Who is running: the open app, a background run
# ---------------------------------------------------------------------------

def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# -- the open app (called by ui_app) --
def app_opened() -> None:
    try:
        _write_json(APP_PRESENCE_FILE, {"pid": os.getpid(), "heartbeat": time.time()})
    except OSError as e:
        logger.log_error(f"Couldn't write {APP_PRESENCE_FILE}: {e}")


app_heartbeat = app_opened


def app_closed() -> None:
    data = _read_json(APP_PRESENCE_FILE)
    if data and data.get("pid") == os.getpid():
        _remove(APP_PRESENCE_FILE)


def app_is_open() -> bool:
    data = _read_json(APP_PRESENCE_FILE)
    if not data or data.get("pid") == os.getpid():
        return False
    fresh = time.time() - float(data.get("heartbeat", 0) or 0) < APP_STALE_SECONDS
    return fresh and pid_alive(data.get("pid"))


def take_request() -> bool:
    """The open app: True (once) when a background run asked it to sync."""
    if not os.path.exists(REQUEST_FILE):
        return False
    _remove(REQUEST_FILE)
    return not os.path.exists(REQUEST_FILE)


# -- a background run --
def background_run() -> Optional[dict]:
    """The state of a background run going on now (another process), else None."""
    data = _read_json(STATE_FILE)
    if not data or data.get("pid") == os.getpid() or not pid_alive(data.get("pid")):
        return None
    return data


def request_stop() -> None:
    """Ask a background run to stop at its next safe point."""
    data = _read_json(STATE_FILE)
    if data:
        data["stop"] = True
        try:
            _write_json(STATE_FILE, data)
        except OSError as e:
            logger.log_error(f"Couldn't ask the scheduled sync to stop: {e}")


# ---------------------------------------------------------------------------
# The result in words
# ---------------------------------------------------------------------------

def summarize(results, errors: int = 0, stopped: bool = False) -> Tuple[str, bool]:
    """(one line for the notification and Settings, ok). results: SyncResults."""
    downloaded = sum(len(r.downloaded) for r in results)
    removed = sum(len(r.removed) for r in results)
    failed = sum(len(r.failed) for r in results)
    parts = []
    if downloaded:
        parts.append(f"{downloaded} new song{'s' if downloaded != 1 else ''}")
    if removed:
        parts.append(f"{removed} removed")
    if failed:
        parts.append(f"{failed} need{'s' if failed == 1 else ''} attention")
    if errors:
        parts.append(f"{errors} playlist{'s' if errors != 1 else ''} couldn't be synced")
    if not parts:
        parts.append("nothing was downloaded" if stopped else "everything was already up to date")
    text = " · ".join(parts)
    text = text[0].upper() + text[1:]
    if stopped:
        text = "Stopped early. " + text
    return text, not (errors or stopped)


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def notify(title: str, text: str, tray=None) -> None:
    """A Windows notification. tray: the open app's QSystemTrayIcon (it
    shows the message and goes on running); without one (a background run)
    a tray icon is made here for NOTIFY_SHOW_SECONDS, and a click on the
    message opens the app."""
    if not get_setting()["notify"]:
        return
    try:
        if tray is not None:
            from PySide6.QtWidgets import QSystemTrayIcon
            tray.show()
            tray.showMessage(title, text, QSystemTrayIcon.Information, NOTIFY_SHOW_SECONDS * 1000)
            return
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon
        app_info.set_windows_app_id()
        app = QApplication.instance() or QApplication([sys.argv[0]])
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = QIcon(app_info.ICON_ICO_PATH) if os.path.exists(app_info.ICON_ICO_PATH) else QIcon()
        t = QSystemTrayIcon(icon)
        t.setToolTip(app_info.APP_NAME)
        t.messageClicked.connect(lambda: (open_app(), app.quit()))
        t.show()
        t.showMessage(title, text, QSystemTrayIcon.Information, NOTIFY_SHOW_SECONDS * 1000)
        QTimer.singleShot(NOTIFY_SHOW_SECONDS * 1000, app.quit)
        app.exec()
        t.hide()
    except Exception as e:  # a notification must never turn a good run into a failed one
        logger.log_error(f"Couldn't show the scheduled sync notification: {e}")


def open_app() -> None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(app_command(), cwd=app_paths.APP_DIR, creationflags=flags, close_fds=True)
    except OSError as e:
        logger.log_error(f"Couldn't open the app from the notification: {e}")


# ---------------------------------------------------------------------------
# The background run
# ---------------------------------------------------------------------------

class _Stopped(Exception):
    pass


def _hand_to_open_app() -> Optional[bool]:
    """The app is open: ask it to sync. True if it took the request, False
    if it didn't answer in time (then nothing is synced: it may be busy or
    hung, and a second writer is never risked)."""
    try:
        _write_json(REQUEST_FILE, {"pid": os.getpid(), "at": time.time()})
    except OSError as e:
        logger.log_error(f"Scheduled sync: couldn't ask the open app to sync ({e}).")
        return False
    deadline = time.time() + REQUEST_WAIT_SECONDS
    while time.time() < deadline:
        if not os.path.exists(REQUEST_FILE):
            return True
        time.sleep(1)
    _remove(REQUEST_FILE)
    return False


def _update_cached_numbers(entry: dict, folder, result) -> None:
    """The Main Menu's cached numbers for this playlist, as the app's own
    Sync writes them (needs Qt for ui_backend; skipped without it)."""
    try:
        import config_manager
        import ui_backend
        stats = ui_backend.compute_local_stats(entry, folder)
        stats.update(
            scanned_at=time.time(),
            missing=ui_backend.missing_records([r.track for r in result.downloaded if r.file_path is None],
                                               config_manager.get_playlist_platform(entry)),
            spotify_unavailable=ui_backend.unavailable_records(result.unavailable_on_spotify),
            **ui_backend.sync_attention(result),
        )
        ui_settings.update_cache(ui_settings.playlist_key(entry), **stats)
    except Exception as e:
        logger.log_error(f"Scheduled sync: couldn't update the cached numbers of '{entry.get('folder_name')}': {e}")


def _sync_everything(state: dict, save_state: Callable[[], None]) -> Tuple[list, int, bool]:
    import artist_watch
    import config_manager
    import library_sync

    config = config_manager.load_config()
    entries = list(config_manager.list_playlists(config))
    for artist_id in artist_watch.watched_artist_ids(config):  # every watched artist, as the launch scan
        added, error = artist_watch.check_artist(config, artist_id)
        if error:
            logger.log_event(f"Scheduled sync: couldn't check an artist for new releases: {error}")
        for e in added:
            logger.log_event(f"Scheduled sync: new release added: {e['folder_name']}")
        entries.extend(added)

    results, errors = [], 0
    last_beat = [0.0]

    def on_progress(msg: str) -> None:
        msg = (msg or "").strip()
        if not msg:
            return
        if _DOWNLOAD_START_RE.match(msg):
            current = _read_json(STATE_FILE) or {}
            if current.get("stop"):
                raise _Stopped()
        state["current"] = msg[:200]
        if time.time() - last_beat[0] > HEARTBEAT_SECONDS:
            last_beat[0] = time.time()
            save_state()

    for n, entry in enumerate(entries, start=1):
        name = entry["folder_name"]
        state.update(playlist=name, index=n, total=len(entries), current="")
        save_state()
        try:
            folder = config_manager.get_playlist_folder(config, entry)
        except ValueError as e:
            logger.log_event(f"Scheduled sync: skipped '{name}': {e}")
            errors += 1
            continue
        try:
            result = library_sync.sync_playlist(entry, folder, on_progress=on_progress)
        except _Stopped:
            logger.log_event(f"Scheduled sync stopped (asked by the app) during '{name}'.")
            return results, errors, True
        except Exception as e:
            logger.log_error(f"Scheduled sync: '{name}' failed: {type(e).__name__}: {e}")
            errors += 1
            continue
        results.append(result)
        _update_cached_numbers(entry, folder, result)
        if (_read_json(STATE_FILE) or {}).get("stop"):
            logger.log_event(f"Scheduled sync stopped (asked by the app) after '{name}'.")
            return results, errors, True
    return results, errors, False


def run_headless() -> int:
    """`--scheduled-sync`: what the Windows task runs. Returns an exit code."""
    logger.log_event(f"Scheduled sync started (v{app_info.VERSION}).")
    if not get_setting()["enabled"]:
        logger.log_event("Scheduled sync is Off in Settings; removing the leftover task.")
        remove_task()
        return 0
    if background_run():
        logger.log_event("Scheduled sync: another one is already running; this one ends.")
        return 0
    if app_is_open():
        taken = _hand_to_open_app()
        if taken:
            logger.log_event("Scheduled sync: handed to the open app.")
            return 0
        summary = "Skipped: the app was open but didn't answer (it will run again tomorrow)."
        record_result(summary, False, in_app=False)
        notify("Scheduled sync skipped", summary)
        return 1

    state = {"pid": os.getpid(), "started": time.time(), "heartbeat": time.time()}

    def save_state() -> None:
        current = _read_json(STATE_FILE) or {}
        state["stop"] = bool(current.get("stop"))
        state["heartbeat"] = time.time()
        try:
            _write_json(STATE_FILE, state)
        except OSError as e:
            logger.log_error(f"Scheduled sync: couldn't write its state file: {e}")

    _remove(STATE_FILE)
    save_state()
    try:
        results, errors, stopped = _sync_everything(state, save_state)
        summary, ok = summarize(results, errors, stopped)
    except Exception as e:
        logger.log_error(f"Scheduled sync failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        summary, ok = f"Couldn't sync: {type(e).__name__}: {e}", False
    finally:
        _remove(STATE_FILE)
    record_result(summary, ok, in_app=False)
    notify("Scheduled sync finished" if ok else "Scheduled sync: needs a look", summary)
    return 0 if ok else 1
