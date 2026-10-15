"""
Mud Music Manager - Application Window (v0.16.6)

Builds the main window: the shared header bar (logo, title, page
subtitle, update status, version, Check For Updates, CMD, Settings), the
page stack (Main Menu, Edit), the in-app overlay that shows Settings and
the Scan / Check Integrity results over the current page (v0.16.13), and
the AppContext the pages share (config dict, background Backend, theme,
library-folder change, config reload).

Launch behaviour (the "never hinder opening" rule): the window appears
immediately from cached numbers; the launch-time library check and the
playlist Scan then run on background threads and fill the screen in as
they finish.

v0.16.6: Python's automatic cyclic garbage collector is switched off and
run from the GUI thread on a timer instead (see _install_gui_thread_gc).

v0.16.17: the launch-time playlist Scan can be turned off in Settings
(Scan on launch); the library check still runs.

v0.16.18: the app is listed in the Windows volume mixer from launch,
not only after a song has played (see MixerPresence).

v0.16.20: keyboard control (Checklist item 27): the window owns the
ui_keys.KeyboardController, which hands key presses to the page that is
showing (the Main Menu so far); G opens and closes Settings.

v0.17.1: branding (Checklist item 52): the emblem is the icon of every
window and of the taskbar button (its own Windows taskbar identity,
app_info.WINDOWS_APP_ID), and the header shows Michael's logo.

v0.19.0 (Checklist item 60b): the launch-time update check and Check For
Updates also ask GitHub whether a newer Mud Music Manager is published
(app_update.py). A newer version comes first in the header's update
message; Check For Updates then shows it (ui_dialogs.AppUpdateDialog)
before the library updates.

v0.21.0 (Checklist item 60e): in the installed app that dialog's Update now
downloads and checks the installer (ui_dialogs.UpdateDownloadDialog), starts
it and closes the app; the installer reopens it (_start_app_update()).

v0.28.0 (Checklist item 60h): updating means installing the newest release
(it bundles the newest Python, libraries, Deno and FFmpeg). At launch and on
Check For Updates update_scan.py asks GitHub for it; nothing pops up:
clicking the version pill opens what the check found
(ui_dialogs.UpdateDetailsDialog), and "Update & Restart MMM" shows when a
newer release can be installed here (installed app or portable copy).
Clicking it downloads the installer / portable .zip with a thin progress bar
under the button (which turns into Cancel), then app_update.launch_update()
installs it and the app closes; the installer / portable swap reopens it.
(Replaces v0.21.0's Update now dialog.) Up to date: the pill stays green and
Check For Updates says so.
"""

import gc
import os
import sys
import threading
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

import app_info
import app_update
import config_manager
import exceptions_manager
import logger
import scheduled_sync
import ui_backend
import ui_cmd
import ui_debug
import ui_dialogs
import ui_keys
import ui_settings
import ui_theme
import update_scan
from ui_edit_page import EditPage
from ui_main_page import MainPage
from ui_results_page import ResultsPage
from ui_settings_page import SettingsPage
from ui_widgets import (
    AppLogo, ClickablePill, GlowBackground, GlyphButton, OverlayHost, Spinner, UpdateRestartBox,
    make_button,
)

try:  # the media backend can be missing on a stripped-down install
    from PySide6.QtMultimedia import QAudioSink, QMediaDevices
    HAVE_MEDIA = True
except Exception:  # pragma: no cover
    HAVE_MEDIA = False

APP_TITLE = "Mud Music Manager"

# Full pages. Settings and the results pages open in the overlay instead
# (v0.16.13), on top of whichever of these is showing.
PAGE_MAIN, PAGE_EDIT = range(2)
SUBTITLES = {
    PAGE_MAIN: "LOCAL SYNC CONSOLE",
    PAGE_EDIT: "EDIT PAGE",
}

GC_INTERVAL_MS = 4000

# Volume mixer entry (v0.16.18): how much silence opens the audio output,
# and how long it runs before being paused.
MIXER_PRIME_US = 50_000
MIXER_PAUSE_MS = 300


# ---------------------------------------------------------------------------
# Garbage collection on the GUI thread only (v0.16.6)
# ---------------------------------------------------------------------------

def _install_gui_thread_gc(app: QApplication) -> None:
    """Python's automatic cyclic GC runs on whichever thread happens to
    allocate enough objects -- during a sync that is the yt-dlp worker
    thread. A collection there can finalize PySide/Qt wrapper objects
    (painters, brushes, paths...) that the GUI thread is using at that
    very moment, which crashed the whole process with a Windows access
    violation (crash_log.txt, 2026-10-05 17:48, Python 3.14). So automatic
    collection is turned off and a full collection is run from a timer in
    the GUI thread instead; worker threads can no longer trigger one.
    Reference-counted objects are still freed immediately as usual; this
    only moves when *cyclic* garbage is swept up."""
    try:
        gc.collect()
        gc.disable()
        timer = QTimer(app)
        timer.setInterval(GC_INTERVAL_MS)
        timer.timeout.connect(gc.collect)
        timer.start()
        app._mmm_gc_timer = timer  # keep a reference for the life of the app
    except Exception as e:  # never stop the app from launching over this
        logger.log_error(f"Couldn't move garbage collection to the GUI thread: {e}")
        try:
            gc.enable()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Windows volume mixer entry from launch (v0.16.18)
# ---------------------------------------------------------------------------

class MixerPresence(QObject):
    """Windows lists an app in its volume mixer only once the app has
    opened an audio output, which used to happen on the first song played
    on the Edit page. This opens one at launch: a few milliseconds of
    silence, then the output is paused and kept open for the life of the
    app. Paused, nothing is sent to the sound card, so it doesn't keep the
    PC awake, but Windows keeps the entry listed. The Edit page player
    joins the same entry (one per app), so the mixer's slider for it works
    with or without a song playing, and is separate from the player's own
    volume slider.

    If the default output device changes (headphones plugged in, etc.)
    the paused output moves to the new one. Any failure is logged and
    ignored: the mixer entry then just appears on the first song, as
    before."""

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._sink = None
        self._device_id = None
        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(self._on_outputs_changed)
        self._open()

    def _open(self) -> None:
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            return
        fmt = device.preferredFormat()
        sink = QAudioSink(device, fmt, self)
        io = sink.start()
        if io is None:
            sink.deleteLater()
            return
        io.write(bytes(fmt.bytesForDuration(MIXER_PRIME_US)))
        self._sink = sink
        self._device_id = bytes(device.id())
        QTimer.singleShot(MIXER_PAUSE_MS, self._pause)

    def _pause(self) -> None:
        if self._sink is not None:
            self._sink.suspend()

    def close(self) -> None:
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:
                pass
            self._sink.deleteLater()
            self._sink = None
            self._device_id = None

    def _on_outputs_changed(self) -> None:
        try:
            device = QMediaDevices.defaultAudioOutput()
            new_id = None if device.isNull() else bytes(device.id())
            if new_id == self._device_id:
                return
            self.close()
            self._open()
        except Exception as e:
            logger.log_error(f"Volume mixer entry: couldn't follow the new output device: {e}")


def _install_mixer_presence(app: QApplication) -> None:
    if not HAVE_MEDIA:
        return
    try:
        presence = MixerPresence(app)
        app.aboutToQuit.connect(presence.close)
        app._mmm_mixer_presence = presence  # keep a reference for the life of the app
    except Exception as e:  # never stop the app from launching over this
        logger.log_error(f"Volume mixer entry: couldn't open an audio output at launch: {e}")


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class AppContext(QObject):
    def __init__(self, version: str):
        super().__init__()
        self.version = version
        self.config = config_manager.load_config()
        self.backend = ui_backend.Backend(self)
        self.window: Optional["MainWindow"] = None
        self.keys: Optional[ui_keys.KeyboardController] = None  # set by MainWindow
        self._mtimes = self._read_mtimes()

    # -- theme --------------------------------------------------------
    def apply_theme(self, name: str) -> None:
        ui_theme.set_theme(name, QApplication.instance())

    # -- config files shared with the terminal menu ----------------------
    @staticmethod
    def _mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    def _read_mtimes(self) -> tuple:
        return (self._mtime(config_manager.CONFIG_PATH), self._mtime(exceptions_manager.EXCEPTIONS_PATH))

    def remember_mtimes(self) -> None:
        self._mtimes = self._read_mtimes()

    def files_changed_elsewhere(self) -> bool:
        return self._read_mtimes() != self._mtimes

    def reload_config(self) -> None:
        """Re-read playlists.json in place (the pages hold this dict)."""
        try:
            fresh = config_manager.load_config()
        except SystemExit as e:
            logger.log_error(f"GUI: couldn't reload the config: {e}")
            return
        self.config.clear()
        self.config.update(fresh)
        self.remember_mtimes()

    def on_config_changed(self) -> None:
        self.remember_mtimes()
        if self.window:
            self.window.main_page.reload()

    # -- Music folder change (runs config_manager.change_library_path) ----
    def change_library(self, new_path: str, move: bool, on_done: Callable[[bool], None]) -> None:
        win = self.window
        busy = ui_dialogs.BusyDialog(win, "Changing the Music folder",
                                     "Moving your playlist files…" if move else "Updating the folder…")
        relay = _LibraryRelay(busy, on_done, win)

        def launch():
            thread = self.backend.run(ui_backend.change_library_task(self.config, new_path, move))
            thread.progress.connect(busy.set_message)
            thread.resultReady.connect(relay.on_result)
            thread.failed.connect(relay.on_failed)

        busy.open()
        self.backend.when_refresh_stopped(launch)


class _LibraryRelay(QObject):
    def __init__(self, busy, on_done, window):
        super().__init__(busy)
        self._busy, self._on_done, self._window = busy, on_done, window

    def on_result(self, result):
        self._busy.finish()
        applied = bool(result.get("applied"))
        if not applied:
            ui_dialogs.warn(self._window, "Music folder not changed",
                            "The folder was not changed. Nothing was moved or lost.")
        self._on_done(applied)

    def on_failed(self, message):
        self._busy.finish()
        ui_dialogs.warn(self._window, "Couldn't change the Music folder", message)
        self._on_done(False)


# ---------------------------------------------------------------------------
# Header bar
# ---------------------------------------------------------------------------

class HeaderBar(QWidget):
    def __init__(self, version: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(26, 18, 26, 10)
        lay.setSpacing(10)

        lay.addWidget(AppLogo(48))  # v0.17.1: Michael's logo (was 44 for the painted mark)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.title = QLabel(APP_TITLE)
        f = self.title.font()
        f.setBold(True)
        f.setPointSizeF(13)
        self.title.setFont(f)
        self.subtitle = QLabel("LOCAL SYNC CONSOLE")
        self.subtitle.setProperty("role", "capsmuted")
        sf = self.subtitle.font()
        sf.setLetterSpacing(QFont.AbsoluteSpacing, 1.8)
        self.subtitle.setFont(sf)
        names.addWidget(self.title)
        names.addWidget(self.subtitle)
        lay.addSpacing(4)
        lay.addLayout(names)
        lay.addStretch(1)

        # update status: spinner + message
        self.spinner = Spinner(18)
        lay.addWidget(self.spinner)
        self.update_msg = QLabel("")
        self.update_msg.setProperty("role", "muted")
        lay.addWidget(self.update_msg)
        lay.addSpacing(6)

        # v0.28.0 (Checklist item 60h): clicking the version opens what the update
        # check found (Michael's choice, replacing a separate "i" button)
        self.version_pill = ClickablePill(f"v{version}", "ok", dot=True, height=30)
        self.version_pill.setToolTip("Click to see what's out of date")
        lay.addWidget(self.version_pill)

        self.update_btn = make_button("Check For Updates")
        lay.addWidget(self.update_btn)
        # v0.28.0: Update & Restart MMM, its "may take a minute" note and progress bar
        self.restart_box = UpdateRestartBox()
        self.restart_btn = self.restart_box.button
        lay.addWidget(self.restart_box)
        self._restart_on = False
        self.restart_box.hide()
        self.cmd_btn = GlyphButton("cmd", 38, boxed=True)
        self.cmd_btn.setToolTip("Open the command line")
        lay.addWidget(self.cmd_btn)
        self.gear_btn = GlyphButton("gear", 38, boxed=True)
        self.gear_btn.setToolTip("Settings")
        lay.addWidget(self.gear_btn)

        self._actions_layout = QHBoxLayout()
        self._actions_layout.setSpacing(10)
        lay.addLayout(self._actions_layout)
        self._actions = []

    def set_subtitle(self, text: str) -> None:
        self.subtitle.setText(text)

    def set_actions(self, widgets: list) -> None:
        for w in self._actions:
            self._actions_layout.removeWidget(w)
            w.hide()
            w.setParent(None)
        self._actions = list(widgets)
        for w in widgets:
            self._actions_layout.addWidget(w)
            w.show()

    def show_update_controls(self, on: bool) -> None:
        self._controls_on = on
        self.update_btn.setVisible(on)
        self.update_msg.setVisible(on)
        self.restart_box.setVisible(on and self._restart_on)

    def set_restart_shown(self, restart: bool) -> None:
        """v0.28.0: Update & Restart MMM (something to download or install);
        only on the Main Menu, like the rest of the update controls."""
        self._restart_on = restart
        self.show_update_controls(getattr(self, "_controls_on", True))

    def set_version_tone(self, tone: str) -> None:
        self.version_pill.set_tone(tone)

    def set_checking(self, on: bool, message: str = "") -> None:
        if on:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.update_msg.setText(message)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        ctx.window = self
        self.setWindowTitle(f"{APP_TITLE} — v{ctx.version}")
        self.resize(1280, 800)
        self.setMinimumSize(1080, 680)
        self._app_update = None  # app_update.AppUpdateStatus, v0.19.0
        self._report = None      # update_scan.UpdateReport, v0.28.0
        self._checking = self._preparing = self._announce = False
        self._update_cancel = {"stop": False}

        bg = GlowBackground()
        self.setCentralWidget(bg)
        root = QVBoxLayout(bg)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.header = HeaderBar(ctx.version)
        root.addWidget(self.header)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.main_page = MainPage(ctx)
        self.edit_page = EditPage(ctx)
        for p in (self.main_page, self.edit_page):
            self.stack.addWidget(p)

        # Settings / results: centred over the dimmed window (header included)
        self.overlay = OverlayHost(bg)
        self.settings_page = SettingsPage(ctx)
        self.results_page = ResultsPage(ctx)
        self.overlay.add_page(self.settings_page)
        self.overlay.add_page(self.results_page)
        self.overlay.closed.connect(self._overlay_closed)

        self.header.cmd_btn.clicked.connect(self.open_cmd)
        self.header.gear_btn.clicked.connect(self.open_settings)
        self.header.update_btn.clicked.connect(self.check_updates)
        self.header.version_pill.clicked.connect(self.show_update_details)  # v0.28.0
        self.header.restart_btn.clicked.connect(self.update_and_restart)
        self.main_page.openEdit.connect(self.open_edit)
        self.main_page.openResults.connect(self.open_results)
        self.main_page.refreshRequested.connect(self.start_refresh)
        self.edit_page.backRequested.connect(lambda: self.go(PAGE_MAIN))
        self.settings_page.closeRequested.connect(self.overlay.close_overlay)
        self.settings_page.checkUpdatesRequested.connect(self.check_updates)
        self.settings_page.restartRequested.connect(self.update_and_restart)
        self.settings_page.updateInfoRequested.connect(self.show_update_details)
        self.results_page.closeRequested.connect(self.overlay.close_overlay)
        self.results_page.changed.connect(self.main_page.reload_stats_only)
        ctx.backend.busyChanged.connect(self.main_page.set_busy)

        self.keys = ui_keys.KeyboardController(self)
        ctx.keys = self.keys

        # v0.23.3: MainPage built its bars a moment ago (its __init__ ends in
        # reload()), so don't build them all a second time (~0.25 s of launch).
        self.go(PAGE_MAIN, rebuild_main=False)

        # v0.25.0 (Checklist item 65): say the app is open (so the scheduled
        # task hands its sync to this window) and take such a request.
        self._tray = None
        self._scheduled_pending_since = None
        self._presence_timer = QTimer(self)
        self._presence_timer.setInterval(scheduled_sync.HEARTBEAT_SECONDS * 1000)
        self._presence_timer.timeout.connect(self._presence_tick)
        self._presence_timer.start()

    # -- navigation -----------------------------------------------------
    def go(self, page: int, rebuild_main: bool = True) -> None:
        current = self.stack.currentIndex()
        if current in (PAGE_EDIT,) and page == PAGE_MAIN:
            self.edit_page.stop_player()
        self.stack.setCurrentIndex(page)
        # v0.16.23: when the old page hides, Qt passes the focus to the
        # next widget in the Tab order, which on the Main Menu is a
        # playlist's position box: a typing cursor appeared there, keys
        # typed into it, and leaving it could reorder that playlist. The
        # new page takes the focus instead (nothing is being typed yet).
        new = self.stack.currentWidget()
        fw = QApplication.focusWidget()
        if fw is None or not (fw is new or new.isAncestorOf(fw)) or ui_keys.is_typing_widget(fw):
            new.setFocus(Qt.OtherFocusReason)
        self.header.set_subtitle(SUBTITLES[page])
        on_main = page == PAGE_MAIN
        self.header.show_update_controls(on_main)
        if page == PAGE_EDIT:
            self.header.set_actions(self.edit_page.header_widgets())
        else:
            self.header.set_actions([])
        if on_main and rebuild_main:
            self.main_page.reload()

    def open_edit(self, entry: dict) -> None:
        self.edit_page.open_entry(entry)
        self.go(PAGE_EDIT)

    # -- overlay (v0.16.13) ---------------------------------------------------
    def open_results(self, kind: str, runs: list) -> None:
        """Scan / Check Integrity results open over whichever page is
        showing (an Edit page with unsaved changes stays as it is)."""
        self.results_page.show_results(kind, runs)
        self.overlay.open_page(self.results_page)

    def open_settings(self) -> None:
        self.settings_page.refresh()
        self.overlay.open_page(self.settings_page)
        # v0.22.0 / v0.22.3: the components line and Project libraries (with Deno and
        # FFmpeg); v0.23.1: read in the background, so Settings opens without a pause
        self.settings_page.refresh_installation()

    def _overlay_closed(self) -> None:
        # v0.23.2: not after Settings. Nothing there changes what the Main Menu shows
        # (Import reloads it itself, data_imported(); the theme repaints by itself), and
        # rebuilding every playlist bar made closing Settings lag (~0.27 s, 20 playlists).
        if self.overlay.pages.currentWidget() is self.settings_page:
            return
        # same refresh the main page got when its Back button returned to it
        if self.stack.currentIndex() == PAGE_MAIN:
            self.main_page.reload()

    # -- Settings → Your data → Import (v0.17.8, Checklist item 59) ------------
    def edit_page_showing(self) -> bool:
        return self.stack.currentIndex() == PAGE_EDIT

    def data_imported(self) -> None:
        """user_data.import_backup() replaced playlists.json, exceptions.json,
        track_failures.json and ui_settings.json (ui_settings is already
        reloaded): pick up the rest. The Edit page closes, since the
        playlist it showed may be gone; then the playlists are checked
        live if Scan on launch is On."""
        self.ctx.reload_config()
        self.ctx.apply_theme(ui_settings.get("theme"))
        if self.keys is not None:
            self.keys.set_keymap(ui_keys.KeyMap(ui_keys.load_keymap()))
        if self.edit_page_showing():
            self.go(PAGE_MAIN)  # reloads the Main Menu
        else:
            self.main_page.reload()
        if ui_settings.get("scan_on_launch"):
            self.start_refresh()

    # -- keyboard (v0.16.20, used by ui_keys.KeyboardController) ---------------
    def key_screen(self):
        """The page whose keys are live: the Main Menu or the Edit page
        (v0.16.23)."""
        return self.edit_page if self.stack.currentIndex() == PAGE_EDIT else self.main_page

    def key_overlay_open(self) -> bool:
        return self.overlay.is_open()

    def key_overlay(self, actions: set, e) -> bool:
        """While the overlay is open only its own keys work (Esc already
        closes it); G closes Settings again."""
        if "settings" in actions and self.overlay.current_page() is self.settings_page:
            if not e.isAutoRepeat():
                self.overlay.close_overlay()
            return True
        return False

    def open_cmd(self) -> None:
        err = ui_cmd.open_cmd(self, on_closed=self._on_cmd_closed)
        if err:
            ui_dialogs.warn(self, "Couldn't open the command line", err)

    def _on_cmd_closed(self) -> None:
        """The CMD window closed (v0.16.7): reload anything the terminal menu
        changed, without waiting for this window to be re-activated. Skipped
        while the app itself is closing."""
        if self.isVisible():
            QTimer.singleShot(0, self._maybe_reload_files)

    # -- startup ----------------------------------------------------------
    def start_background_work(self) -> None:
        """Called once the window is on screen. The playlist Scan is
        skipped when Settings → Scan on launch is Off (v0.16.17)."""
        self.run_update_check(announce=False)  # v0.28.0: everything, at every launch
        if scheduled_sync.supported() and scheduled_sync.get_setting()["enabled"]:
            # v0.25.0: re-create the task, so it points at this copy (moved, updated)
            threading.Thread(target=scheduled_sync.apply, name="scheduled-sync-task", daemon=True).start()
        if ui_settings.get("scan_on_launch"):
            self.start_refresh()
        else:
            self.main_page.show_launch_scan_skipped()

    def start_refresh(self) -> None:
        # v0.16.50 (Checklist item 41c): watched artists are checked for new
        # releases too; the Main Menu adds them on the GUI thread.
        thread = self.ctx.backend.start_refresh(self.ctx.config, self.main_page.apply_new_releases_call)
        if thread is None:
            return
        self.main_page.set_refreshing(True)
        thread.localStats.connect(self.main_page.on_local_stats)
        thread.entryStarted.connect(self.main_page.on_entry_started)
        thread.entryDone.connect(self.main_page.on_entry_done)
        thread.resultReady.connect(self._refresh_finished)
        thread.cancelled.connect(self._refresh_finished)  # "Cancel Scan, Use Cache" (v0.16.14)
        thread.failed.connect(self._refresh_finished)

    def _refresh_finished(self, *_args) -> None:
        self.main_page.on_refresh_finished()
        self.main_page.reload_stats_only()

    # -- updates (v0.28.0, Checklist item 60h: update_scan.py) -----------------
    def check_updates(self) -> None:
        self.run_update_check(announce=True)

    def run_update_check(self, announce: bool) -> None:
        """Ask GitHub for the latest release on a worker thread and show the
        result; nothing is downloaded until Update & Restart MMM. announce:
        Check For Updates was pressed, so "up to date" gets a message."""
        if self._checking or self._preparing:
            return
        self._checking = True
        self._announce = announce
        self.header.set_checking(True, "Checking for updates…")
        self._enable_update_buttons(False)
        thread = self.ctx.backend.run(ui_backend.update_scan_task(), heavy=False)
        thread.resultReady.connect(self._on_scan_result)
        thread.failed.connect(self._on_update_failed)

    def _enable_update_buttons(self, on: bool) -> None:
        self.header.update_btn.setEnabled(on)
        self.settings_page.update_btn.setEnabled(on)

    def _on_update_failed(self, message: str) -> None:
        self._checking = False
        self._enable_update_buttons(True)
        self.header.set_checking(False, "Couldn't check for updates")
        self.header.set_version_tone("warn")
        logger.log_error(f"Update check failed: {message}")

    def _on_scan_result(self, report) -> None:
        self._report = report
        self._app_update = report.app
        self._checking = False
        self._enable_update_buttons(True)
        view = self._show_update_view()
        if self._announce and not view.out_of_date:  # as before: say it's all current
            ui_dialogs.info(self, "Check For Updates", "Mud Music Manager is up to date."
                            if view.message == "Up to date" else f"{view.message}.")

    def _show_update_view(self):
        """The header and Settings → Updates: message, pill and Update &
        Restart MMM, from the last check."""
        view = update_scan.summary(self._report, preparing=self._preparing)
        self.header.set_checking(self._preparing, view.message)
        self.header.set_version_tone(view.tone)
        self.header.version_pill.setToolTip(f"{view.message} — click for details")
        self.header.set_restart_shown(view.restart_ready or self._preparing)
        self.settings_page.set_update_state(view.message, view.restart_ready or self._preparing)
        return view

    def _set_update_progress(self, fraction) -> None:
        for box in (self.header.restart_box, self.settings_page.restart_box):
            box.set_progress(fraction)
            box.button.setEnabled(True)  # during a download it is the Cancel button
            box.button.setText("Cancel" if (fraction is not None and fraction < 1.0) else "Update && Restart MMM")

    def show_update_details(self) -> None:
        """The version pill: what the last check found."""
        if self._report is None:
            ui_dialogs.info(self, "Updates", "Still checking for updates; try again in a moment.")
            return
        view = update_scan.summary(self._report, preparing=self._preparing)
        app = self._report.app
        dlg = ui_dialogs.UpdateDetailsDialog(
            self, update_scan.details_markdown(self._report), view.restart_ready,
            page_url=app.page_url if app.outdated and not app.can_self_update() else "")
        dlg.exec()
        if dlg.choice == "restart":
            self.update_and_restart()

    def update_and_restart(self) -> None:
        """Update & Restart MMM: download the newer release (installer or
        portable .zip, checked against its fingerprints) with the thin bar
        under the button following it, then install it and reopen the app.
        While it downloads the button is Cancel."""
        if self._preparing:  # the button reads Cancel now
            self._update_cancel["stop"] = True
            return
        if self._report is None or self._checking or not self._report.app.can_self_update():
            return
        if self.ctx.backend.heavy_running:
            ui_dialogs.warn(self, "Update after this finishes", "Finish or cancel the running Sync, Scan or check first.")
            return
        if self.stack.currentIndex() == PAGE_EDIT and not self.edit_page.confirm_leave():
            return
        self._preparing = True
        self._update_cancel = {"stop": False}
        self._enable_update_buttons(False)
        self._show_update_view()
        self._set_update_progress(0.0)
        thread = self.ctx.backend.run(ui_backend.download_update_task(self._report.app, self._update_cancel),
                                      heavy=False)
        thread.progress.connect(self._on_update_progress)
        thread.resultReady.connect(self._on_update_downloaded)
        thread.failed.connect(self._on_update_download_failed)

    def _on_update_progress(self, text: str) -> None:
        """'done/total' bytes from the download thread."""
        try:
            done, total = (int(x) for x in text.split("/"))
        except ValueError:
            return
        if total:
            self._set_update_progress(min(done / total, 0.99))

    def _on_update_download_failed(self, message: str) -> None:
        self._preparing = False
        self._enable_update_buttons(True)
        self._set_update_progress(None)
        self._show_update_view()
        if message.startswith("UpdateError: "):  # TaskThread prefixes the exception's type
            message = message[len("UpdateError: "):]
        if not self._update_cancel.get("stop"):
            ui_dialogs.warn(self, "Couldn't update", message)

    def _on_update_downloaded(self, path: str) -> None:
        self._set_update_progress(1.0)  # done: the bar stays full as the app closes
        self.header.set_checking(False, "Installing the update…")
        try:
            app_update.launch_update(path)
        except app_update.UpdateError as e:
            self._on_update_download_failed(str(e))
            return
        # the installer / the portable swap reopens the app; close it now
        QTimer.singleShot(800, lambda: (self.close(), QApplication.instance().quit()))

    # -- shared-file reload ---------------------------------------------------
    def changeEvent(self, e):
        if e.type() == QEvent.ActivationChange and self.isActiveWindow():
            QTimer.singleShot(0, self._maybe_reload_files)
        super().changeEvent(e)

    def _maybe_reload_files(self) -> None:
        """The terminal menu / CMD window edits the same JSON files. When
        the window comes back to the front, pick those changes up."""
        if self.ctx.backend.heavy_running or not self.ctx.files_changed_elsewhere():
            return
        self.ctx.reload_config()
        editing = self.stack.currentIndex() == PAGE_EDIT
        if editing and self.edit_page.has_unsaved():
            self.main_page.reload()
            return
        self.main_page.reload()
        if editing and self.edit_page.entry:
            fresh = config_manager.find_playlist(self.ctx.config, self.edit_page.entry["folder_name"])
            if fresh:
                self.edit_page.open_entry(fresh)
            else:
                self.go(PAGE_MAIN)

    # -- closing -----------------------------------------------------------------
    # -- scheduled sync (v0.25.0, Checklist item 65) ---------------------------
    SCHEDULED_WAIT_RETRY_MS = 30_000
    SCHEDULED_WAIT_GIVE_UP_S = 3 * 3600

    def _presence_tick(self) -> None:
        scheduled_sync.app_heartbeat()
        if scheduled_sync.take_request():
            logger.log_event("Scheduled sync: the open app takes it over.")
            self.run_scheduled_sync()

    def run_scheduled_sync(self) -> None:
        """Sync every playlist now, as the scheduled task asked (or Settings'
        Try it now). Waits while something else runs or the Edit page is
        open, trying again every 30 s for up to 3 hours."""
        import time as _time
        if self._scheduled_pending_since is None:
            self._scheduled_pending_since = _time.time()
        busy = self.ctx.backend.heavy_running or self.stack.currentIndex() == PAGE_EDIT
        if busy:
            if _time.time() - self._scheduled_pending_since > self.SCHEDULED_WAIT_GIVE_UP_S:
                self._scheduled_pending_since = None
                summary = "Skipped: the app stayed busy for 3 hours."
                scheduled_sync.record_result(summary, False, in_app=True)
                scheduled_sync.notify("Scheduled sync skipped", summary, self.tray_icon())
                return
            QTimer.singleShot(self.SCHEDULED_WAIT_RETRY_MS, self.run_scheduled_sync)
            return
        self._scheduled_pending_since = None
        if self.overlay.isVisible():
            self.overlay.close_overlay()
        problem = self.main_page.start_scheduled_sync()
        if problem:
            scheduled_sync.record_result(problem, False, in_app=True)
            scheduled_sync.notify("Scheduled sync skipped", problem, self.tray_icon())

    def tray_icon(self):
        """The icon a scheduled sync's notification comes from; shown only
        for the notification (hidden again a little after)."""
        from PySide6.QtWidgets import QSystemTrayIcon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        if self._tray is None:
            self._tray = QSystemTrayIcon(self.windowIcon(), self)
            self._tray.setToolTip(APP_TITLE)
            self._tray.messageClicked.connect(self._show_from_tray)
        QTimer.singleShot((scheduled_sync.NOTIFY_SHOW_SECONDS + 5) * 1000, self._tray.hide)
        return self._tray

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self.stack.currentIndex() == PAGE_EDIT and not self.edit_page.confirm_leave():
            e.ignore()
            return
        self.edit_page.stop_player()
        ui_cmd.close_cmd()
        ui_debug.close_debug()
        self.ctx.backend.shutdown()
        super().closeEvent(e)


# ---------------------------------------------------------------------------
# Entry point used by main.py
# ---------------------------------------------------------------------------

def _app_icon() -> QIcon:
    """The emblem as the app's icon (v0.17.1, Checklist item 52). The .ico
    holds versions drawn for 16-256 px, so small places (title bar,
    taskbar) get a sharp one; the PNG is the fallback."""
    icon = QIcon()
    if os.path.exists(app_info.ICON_ICO_PATH):
        icon = QIcon(app_info.ICON_ICO_PATH)
    if icon.isNull() and os.path.exists(app_info.ICON_PNG_PATH):
        icon = QIcon(app_info.ICON_PNG_PATH)
    return icon


def run(version: str) -> int:
    """Start the graphical app. Returns the process exit code."""
    ui_settings.get("theme")  # load/repair the UI settings file up front
    # v0.17.1: own taskbar identity, set before any window exists, so the
    # taskbar shows the app's icon rather than Python's.
    app_info.set_windows_app_id()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationVersion(version)
    app.setOrganizationName(app_info.OWNER)
    icon = _app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)  # every window and dialog inherits it
    _install_mixer_presence(app)
    ui_theme.init_from_settings()
    ui_theme.apply_app_font(app)
    app.setStyleSheet(ui_theme.build_stylesheet())
    _install_gui_thread_gc(app)

    # v0.25.0 (Checklist item 65): a scheduled sync running in the background
    # finishes (or is stopped) before the app opens, so only one copy syncs.
    if scheduled_sync.background_run() is not None:
        if ui_dialogs.BackgroundSyncDialog().exec() != ui_dialogs.BaseDialog.Accepted:
            return 0
    scheduled_sync.app_opened()
    app.aboutToQuit.connect(scheduled_sync.app_closed)

    ctx = AppContext(version)
    window = MainWindow(ctx)
    window.show()
    QTimer.singleShot(50, window.start_background_work)
    return app.exec()
