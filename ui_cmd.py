"""
Mud Music Manager - CMD Subpage (v0.16.7)

The fail-safe command-line route into the app. Two behaviours, chosen by
the Settings toggle (ui_settings "cmd_mode"):

  window   -> a separate console window (this module's CmdWindow) that
              runs the classic terminal menu (`main.py --cli`) in a child
              process and shows its output / takes typed input.
  terminal -> a real operating-system terminal running the same menu.

Either way it is the genuine terminal menu from main.py, so it keeps
working even if the graphical screens break. The child process is a
separate program: when it changes playlists.json / exceptions.json the
GUI notices (file modification times) and reloads.

v0.16.7 (Checklist item 11): the in-app window marks its child with
MMM_CLI_EMBEDDED so main.py runs the menu over the window's pipes instead of
opening an OS terminal (Qt starts the child with no console window, which
main.py used to read as "no terminal"). Both modes now close their window
when the menu exits (`done`, or any other exit).

v0.17.9 (Checklist item 59): both modes set MMM_CLI_FROM_GUI on the menu,
so its Settings -> Import is refused while the app is open (the app holds
ui_settings in memory and would write it back over the imported settings).
"""

import os
import shlex
import shutil
import subprocess
import sys
from typing import Optional

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget,
)

import debug_console
import logger
import ui_settings
import ui_theme
from ui_widgets import make_button

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(BASE_DIR, "main.py")


def cli_command() -> list:
    # v0.16.3: under pythonw.exe (no console) the menu needs python.exe.
    return [debug_console.console_python_executable(), "-u", MAIN_SCRIPT, "--cli"]


# ---------------------------------------------------------------------------
# Real OS terminal
# ---------------------------------------------------------------------------

def launch_os_terminal() -> Optional[str]:
    """Open an OS terminal running the classic menu. Returns None on
    success, otherwise a short explanation of what went wrong."""
    cmd = cli_command()
    # v0.17.9: tell the menu the app is open beside it. Set inside the shell
    # command on macOS / Linux, because a terminal program may not pass its
    # own environment on to the shell it starts.
    marker = f"{debug_console.CLI_FROM_GUI_ENV}=1"
    try:
        if sys.platform.startswith("win"):
            env = dict(os.environ)
            env[debug_console.CLI_FROM_GUI_ENV] = "1"
            subprocess.Popen(cmd, cwd=BASE_DIR, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
            return None
        if sys.platform == "darwin":
            # `; exit` ends the shell with the menu so Terminal can close the
            # window (whether it does is Terminal's own profile setting).
            shell_cmd = "cd {} && {} {}; exit".format(shlex.quote(BASE_DIR), marker,
                                                      " ".join(shlex.quote(c) for c in cmd))
            script = 'tell application "Terminal" to do script "{}"\ntell application "Terminal" to activate'.format(
                shell_cmd.replace("\\", "\\\\").replace('"', '\\"'))
            subprocess.Popen(["osascript", "-e", script])
            return None
        # v0.16.7: no "Press Enter to close…" pause - the terminal closes when the menu exits.
        inner = "cd {} && {} {}".format(
            shlex.quote(BASE_DIR), marker, " ".join(shlex.quote(c) for c in cmd))
        candidates = [
            ("x-terminal-emulator", ["-e", "bash", "-c", inner]),
            ("gnome-terminal", ["--", "bash", "-c", inner]),
            ("konsole", ["-e", "bash", "-c", inner]),
            ("xfce4-terminal", ["-e", "bash -c {}".format(shlex.quote(inner))]),
            ("kitty", ["bash", "-c", inner]),
            ("alacritty", ["-e", "bash", "-c", inner]),
            ("xterm", ["-e", "bash", "-c", inner]),
        ]
        for exe, args in candidates:
            if shutil.which(exe):
                subprocess.Popen([exe] + args, cwd=BASE_DIR)
                return None
        return "No supported terminal program was found. Switch the CMD button to “App window” in Settings."
    except Exception as e:
        logger.log_error(f"GUI: couldn't open an OS terminal: {e}")
        return f"Couldn't open a terminal: {e}"


# ---------------------------------------------------------------------------
# In-app console window
# ---------------------------------------------------------------------------

class CmdWindow(QWidget):
    """Separate top-level window hosting the classic menu."""

    closedByUser = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Mud Music Manager — CMD")
        self.resize(900, 620)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setObjectName("cmdwindow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("Command line")
        f = title.font()
        f.setBold(True)
        f.setPointSizeF(13)
        title.setFont(f)
        top.addWidget(title)
        self.state = QLabel("")
        self.state.setProperty("role", "muted")
        top.addWidget(self.state, 1)
        self.restart_btn = make_button("Restart menu", compact=True)
        self.restart_btn.clicked.connect(self.restart)
        self.clear_btn = make_button("Clear", compact=True)
        self.clear_btn.clicked.connect(lambda: self.out.clear())
        top.addWidget(self.clear_btn)
        top.addWidget(self.restart_btn)
        lay.addLayout(top)

        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(ui_theme.mono_font(10))
        self.out.setMaximumBlockCount(5000)
        lay.addWidget(self.out, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.prompt = QLabel(">")
        self.prompt.setFont(ui_theme.mono_font(11))
        row.addWidget(self.prompt)
        self.input = QLineEdit()
        self.input.setFont(ui_theme.mono_font(10.5))
        self.input.setPlaceholderText("Type here and press Enter — same as the terminal menu")
        self.input.returnPressed.connect(self._send)
        row.addWidget(self.input, 1)
        self.send_btn = make_button("Send", "primary", compact=True)
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.send_btn)
        lay.addLayout(row)

        self.proc: Optional[QProcess] = None
        self._history = []
        self._hist_i = 0
        self.start()

    # -- process ----------------------------------------------------------
    def start(self) -> None:
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            return
        self.proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert(debug_console.CLI_EMBEDDED_ENV, "1")  # run the menu here, not in an OS terminal (v0.16.7)
        env.insert(debug_console.CLI_FROM_GUI_ENV, "1")  # v0.17.9: the app is open beside the menu
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(BASE_DIR)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyRead.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        cmd = cli_command()
        self.proc.start(cmd[0], cmd[1:])
        self.state.setText("running")
        self.input.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.input.setFocus()

    def restart(self) -> None:
        self.stop()
        self.out.appendPlainText("\n— restarting the menu —\n")
        self.start()

    def stop(self) -> None:
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.proc.finished.disconnect(self._finished)
            self.proc.kill()
            self.proc.waitForFinished(1500)
        self.proc = None

    def _read(self) -> None:
        if self.proc is None:
            return
        text = bytes(self.proc.readAll()).decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        self._append(text)

    def _append(self, text: str) -> None:
        cursor = self.out.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self.out.setTextCursor(cursor)
        self.out.ensureCursorVisible()

    def _finished(self, code, _status) -> None:
        """The menu exited (`done`, or any other reason): close the window
        (v0.16.7, Checklist item 11). An abnormal exit would otherwise vanish
        with the window, so the end of its output goes to the error log."""
        self._read()
        self.state.setText("not running")
        self.input.setEnabled(False)
        self.send_btn.setEnabled(False)
        if code != 0:
            tail = "\n".join(self.out.toPlainText().strip().splitlines()[-40:])
            logger.log_error(f"GUI: CMD window menu exited with code {code}. Last output:\n{tail}")
        QTimer.singleShot(0, self.close)  # after this signal handler returns

    def _error(self, _err) -> None:
        if self.proc is not None:
            self._append(f"\n[couldn't start the menu: {self.proc.errorString()}]\n")
            self.state.setText("couldn't start")

    def _send(self) -> None:
        if self.proc is None or self.proc.state() != QProcess.Running:
            return
        text = self.input.text()
        self.input.clear()
        self._append(text + "\n")
        self.proc.write((text + "\n").encode("utf-8"))

    def closeEvent(self, e):
        self.stop()
        self.closedByUser.emit()
        super().closeEvent(e)


_window: Optional[CmdWindow] = None


def open_cmd(parent, on_closed=None) -> Optional[str]:
    """Honor the Settings toggle. Returns an error message or None.
    on_closed (optional) is called when the in-app window closes, so the
    caller can pick up any playlists.json / exceptions.json changes (v0.16.7)."""
    global _window
    if ui_settings.get("cmd_mode") == ui_settings.CMD_TERMINAL:
        return launch_os_terminal()
    if _window is None:
        _window = CmdWindow()
        _window.closedByUser.connect(_forget)
        if on_closed is not None:
            _window.closedByUser.connect(on_closed)
    _window.show()
    _window.raise_()
    _window.activateWindow()
    return None


def _forget() -> None:
    global _window
    _window = None


def close_cmd() -> None:
    global _window
    if _window is not None:
        _window.close()
        _window = None
