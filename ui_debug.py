"""
Mud Music Manager - Debug Window (v0.16.3)

Settings -> "Open Debug Window". On Windows this opens a real console window
(started by debug_console.py as a separate helper process, so closing it can
never close the app). Everywhere else - or if that console can't be opened -
it shows an in-app log window instead. Both show the same thing: the app's
log lines, the printed output of the engine / background tasks, and Qt's own
messages (the last 3000 lines are kept, so opening it late still shows
recent history).
"""

from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

import debug_console
import logger
import ui_theme
from ui_widgets import make_button


class _LineBridge(QObject):
    """Lines arrive from any thread; this hands them to the GUI thread."""
    line = Signal(str)


class DebugLogWindow(QWidget):
    """Separate top-level window showing the live log (non-Windows fallback)."""

    closedByUser = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Mud Music Manager — Debug Window")
        self.resize(900, 560)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setObjectName("cmdwindow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("Debug window")
        f = title.font()
        f.setBold(True)
        f.setPointSizeF(13)
        title.setFont(f)
        top.addWidget(title)
        hint = QLabel("Live log and printed output. Closing this window does not close the app.")
        hint.setProperty("role", "muted")
        top.addWidget(hint, 1)
        clear = make_button("Clear", compact=True)
        clear.clicked.connect(lambda: self.out.clear())
        close = make_button("Close", compact=True)
        close.clicked.connect(self.close)
        top.addWidget(clear)
        top.addWidget(close)
        lay.addLayout(top)

        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(ui_theme.mono_font(10))
        self.out.setMaximumBlockCount(debug_console.MAX_LINES)
        lay.addWidget(self.out, 1)

        self._bridge = _LineBridge(self)
        self._bridge.line.connect(self._append)
        self._listener = self._bridge.line.emit  # thread-safe: queued to the GUI thread
        self._listening = False
        self._attach()

    def _attach(self) -> None:
        if self._listening:
            return
        history = debug_console.add_listener(self._listener)
        self._listening = True
        if history:
            self.out.setPlainText("\n".join(history))
            self._scroll_end()

    def _detach(self) -> None:
        if self._listening:
            debug_console.remove_listener(self._listener)
            self._listening = False

    def _append(self, line: str) -> None:
        bar = self.out.verticalScrollBar()
        stick = bar.value() >= bar.maximum() - 4  # only follow if already at the bottom
        self.out.appendPlainText(line)
        if stick:
            self._scroll_end()

    def _scroll_end(self) -> None:
        cursor = self.out.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.out.setTextCursor(cursor)
        self.out.ensureCursorVisible()

    def reopen(self) -> None:
        self._attach()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        self._detach()
        self.closedByUser.emit()
        super().closeEvent(e)


_window: Optional[DebugLogWindow] = None


def uses_real_console() -> bool:
    """True where the Debug Window is a real console (Windows)."""
    return debug_console.IS_WINDOWS


def is_open() -> bool:
    if debug_console.external_console_open():
        return True
    return _window is not None and _window.isVisible()


def open_debug(parent=None) -> Optional[str]:
    """Open the Debug Window. Returns None normally; a short message if the
    real console couldn't be opened and the in-app window was used instead."""
    global _window
    note = None
    if debug_console.IS_WINDOWS:
        err = debug_console.open_external_console()
        if err is None:
            return None
        logger.log_error(f"Debug Window: {err}")
        note = f"{err} Showing the log inside the app instead."
    if _window is None:
        _window = DebugLogWindow()
    _window.reopen()  # the same window object is reused every time
    return note


def close_debug() -> None:
    """Close whichever Debug Window is open (also called when the app exits,
    so a leftover top-level window can't keep the process alive)."""
    debug_console.close_external_console()
    if _window is not None:
        _window.close()
