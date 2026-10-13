"""
Mud Music Manager (SpotDL) - Logger

Writes errors and session events to log files instead of letting them
disappear from the terminal once it's closed. Since v0.18.0 the logs folder
is in the user's data folder (app_paths.DATA_DIR), not the app folder.
"""

import os
from datetime import datetime

import app_paths
import debug_console

LOG_DIR = app_paths.data_path("logs")
ERROR_LOG_PATH = os.path.join(LOG_DIR, "error_log.txt")
EVENT_LOG_PATH = os.path.join(LOG_DIR, "session_log.txt")


def _ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mirror(line):
    """Also show the line in the Debug Window's buffer (v0.16.3). Never
    allowed to interfere with logging itself."""
    try:
        debug_console.write_line(line.rstrip("\n"))
    except Exception:
        pass


def log_error(message):
    """Append a timestamped error line to logs/error_log.txt."""
    _ensure_log_dir()
    line = f"[{_timestamp()}] ERROR: {message}\n"
    with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
    _mirror(line)


def log_event(message):
    """Append a timestamped line to logs/session_log.txt for normal
    events worth keeping a record of (not errors)."""
    _ensure_log_dir()
    line = f"[{_timestamp()}] {message}\n"
    with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
    _mirror(line)
