"""
Mud Music Manager - double-click launcher (v0.16.5)

Double-clicking a .pyw file runs it under pythonw.exe, which has no console,
so the app starts directly instead of starting under python.exe and then
handing over to a second pythonw.exe copy (what double-clicking main.py does).
This file only runs main.py exactly as `python main.py` would; nothing else
lives here. Keep it in the same folder as main.py.
"""

import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# There is no console to hide, so main.py must not try to relaunch itself.
os.environ.setdefault("MMM_GUI_RELAUNCHED", "1")

try:
    runpy.run_path(os.path.join(_HERE, "main.py"), run_name="__main__")
except SystemExit:
    raise
except BaseException as _e:  # nothing would be visible without a console
    try:
        import traceback
        _text = traceback.format_exc()
        try:  # v0.18.0: the logs folder is in the user's data folder
            import app_paths
            _logs = app_paths.data_path("logs")
        except Exception:
            _logs = os.path.join(_HERE, "logs")
        os.makedirs(_logs, exist_ok=True)
        with open(os.path.join(_logs, "error_log.txt"), "a", encoding="utf-8") as _f:
            _f.write(f"Launcher: uncaught exception:\n{_text}\n")
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, _text[-1500:], "Mud Music Manager", 0x10)
    except Exception:
        pass
    raise
