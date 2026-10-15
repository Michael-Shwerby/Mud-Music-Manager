"""
Mud Music Manager - Settings Subpage (v0.16.0)

Appearance (dark / light), what the CMD button does, the Music folder
(read-only here; changed from the main menu), the Project Libraries
section (every package in requirements.txt with its version and the
folder it is installed in), Check For Updates, and (v0.16.3) the Debug Window
button that opens / closes the live log console.

v0.16.13: shown in the in-app overlay over the current screen, with its
own close button in the title row (fixed above the scrolling content).

v0.16.17: Scan on launch (On / Off) -- whether the app checks every
playlist live each time it opens (ui_settings "scan_on_launch").

v0.16.24: keyboard (Checklist item 27 part 3): W/A/S/D move between the
buttons and choices, Space selects (handled by ui_keys).

v0.16.26: Keyboard -> Remap keyboard controls (ui_remap.RemapDialog,
Checklist item 27 part 5).

v0.16.42: YouTube sign-in (Checklist item 48): Off / Browser (pick one) /
cookies.txt (pick the file), and a Test button that reads the cookies in
the background and says whether they hold a signed-in YouTube session.
The setting itself lives in youtube_cookies.py (ui_settings.json).

v0.16.44: Download quality (Checklist item 39): file format (v0.16.45), source audio,
conversion bitrate, speed limit and a pause before each download, each a ▾ menu,
plus "Reset to defaults". Only here: nothing is asked before a download.
The setting itself lives in download_settings.py (ui_settings.json).

v0.17.4: Smooth scrolling (On / Off, Checklist item 55), under Scan on
launch (ui_settings "smooth_scroll", read live by ui_widgets). Download
quality also has Cover art (v0.17.3).

v0.17.7: Prefer explicit versions (On / Off, Checklist item 58), under
Smooth scrolling (ui_settings "prefer_explicit", read live by
youtube_matcher).

v0.17.8: Your data (Checklist item 59), under Keyboard: Export… saves the
playlists, exceptions, settings and pictures to one backup file; Import…
checks a backup, confirms (ui_dialogs.ImportDataDialog) and replaces this
computer's data with it (user_data.py; the old data is saved first).
v0.18.1: under them, the Data folder line shows where the app keeps the
user's data (app_paths.DATA_DIR, v0.18.0), full path in its tooltip.
v0.18.2: that path can be selected and copied (drag, or right-click → Copy).

v0.17.1: About (Checklist item 52), the last panel: the logo, name,
version, copyright and trademark lines, the platform disclaimer, and
License / Third-party notices buttons (LICENSE.txt, THIRD_PARTY_NOTICES.txt).
v0.27.0: under the About panel, a Support Mud Music Manager button opens
the Support window (ui_dialogs.SupportDialog), as the Main Menu's heart does.

v0.23.0 (Michael's request): the settings are split into pages, picked
with tabs under the title (show_page()), each page its own scroll area:
  Preferences -- the Visuals group (Appearance, Smooth scrolling), then
    CMD button, Scan on launch, Debug window;
  Download Settings -- Prefer explicit versions, YouTube sign-in,
    Download quality (no group);
  User Data & Installation -- the User Data group (Keyboard, Music
    folder, Your data + Data folder) and the User Installation group
    (Updates, Project libraries).
About is a fourth page whose tab sits on its own at the far right. Each
group has a painted icon (ui_widgets.GroupIcon). The sheet is larger
than before (OVERLAY_SIZE_FRACTION), and each page scrolls sideways
rather than cutting a row off if the window is ever too narrow for it.
The page last shown stays selected until the app closes.

v0.23.1: a wider gap after each group, so the settings that follow one
read as separate. The components line and Project libraries are read on
a worker thread each time Settings opens (refresh_installation()); done
on the GUI thread they froze the app ~0.6 s before the sheet appeared.

v0.24.0: Playlist files (On / Off, Checklist item 64), in the User Data
group under Music folder (ui_settings "playlist_files", read by
playlist_file.py at each Sync).

v0.25.0: Scheduled sync (Checklist item 65), its own group (clock icon)
on the Download Settings page: On / Off, the time, the notification,
Try it now, and the last run (scheduled_sync.py).

v0.23.2 (Michael's change of mind): every setting is now in a group.
Preferences' CMD button, Scan on launch and Debug window are the Other
group; the Download Settings page's three are one group, Download
Settings. Both have icons (ui_widgets.GroupIcon 'other', 'download').
"""

import html
import os

from PySide6.QtCore import QPoint, QStandardPaths, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMenu, QScrollArea, QSizePolicy, QStackedWidget, QTimeEdit, QVBoxLayout,
    QWidget,
)

import app_info
import app_paths
import component_updates
import config_manager
import download_settings
import scheduled_sync
import ui_debug
import ui_dialogs
import ui_libraries
import ui_remap
import ui_settings
import ui_theme
import user_data
import youtube_cookies
from ui_widgets import (
    AppLogo, CapsLabel, ElidedLabel, GlyphButton, GroupIcon, SegmentedControl, UpdateRestartBox, make_button,
    make_panel, smooth_scroll,
)


def _set_role(w: QWidget, role: str) -> None:
    w.setProperty("role", role)
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


class SettingsPage(QWidget):
    closeRequested = Signal()
    themeChanged = Signal(str)
    checkUpdatesRequested = Signal()
    restartRequested = Signal()      # v0.28.0: Update & Restart MMM
    updateInfoRequested = Signal()   # v0.28.0: the Details link (what the check found)

    # v0.23.0: a larger sheet than the results pages' two-thirds (ui_widgets.OverlayHost)
    OVERLAY_SIZE_FRACTION = 0.86

    # v0.23.0: the pages, in tab order; About sits apart at the far right
    PAGE_PREFERENCES = "preferences"
    PAGE_DOWNLOADS = "downloads"
    PAGE_USER = "user"
    PAGE_ABOUT = "about"

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._install_reading = False   # v0.23.1: refresh_installation()
        self._install_again = False
        self._install_reread = False
        self._libs_shown = None
        self.close_btn = GlyphButton("close", 36, boxed=True)
        self.close_btn.setToolTip("Close (Esc)")
        self.close_btn.clicked.connect(self.closeRequested.emit)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 20, 22, 22)
        outer.setSpacing(16)

        title_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title_box.addWidget(CapsLabel("Application"))
        t = QLabel("Settings")
        t.setProperty("role", "title")
        title_box.addWidget(t)
        title_row.addLayout(title_box)
        title_row.addStretch(1)
        title_row.addWidget(self.close_btn, 0, Qt.AlignTop)
        outer.addLayout(title_row)

        # ---- Page tabs (v0.23.0): the three settings pages on the left, About on its own at the right
        nav = QHBoxLayout()
        nav.setSpacing(12)
        self.nav_ctl = SegmentedControl(
            [(self.PAGE_PREFERENCES, "Preferences"), (self.PAGE_DOWNLOADS, "Download Settings"),
             (self.PAGE_USER, "User Data && Installation")],
            self.PAGE_PREFERENCES)
        self.nav_ctl.changed.connect(self.show_page)
        nav.addWidget(self.nav_ctl, 0, Qt.AlignVCenter)
        nav.addStretch(1)
        self.about_ctl = SegmentedControl([(self.PAGE_ABOUT, "About")], "")
        self.about_ctl.changed.connect(self.show_page)
        nav.addWidget(self.about_ctl, 0, Qt.AlignVCenter)
        outer.addLayout(nav)

        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)
        self._page_index = {}

        # ---- Preferences
        col = self._add_page(self.PAGE_PREFERENCES)
        self._add_group(col, "visuals", "Visuals", "How the app looks, and how its lists move.", [
            self._row_panel(
                "Appearance", "Dark gray is the default. Switch to a light look any time.",
                self._make_theme_control()),
            # v0.17.4, Checklist item 55
            self._row_panel(
                "Smooth scrolling", "Lists glide when you scroll with the mouse wheel or keyboard, and when an "
                "opened playlist is brought into view. Off, they jump straight there. Applies at once.",
                self._make_smooth_scroll_control()),
        ])
        # v0.23.2: grouped as Other (were ungrouped in v0.23.0-v0.23.1)
        self._add_group(col, "other", "Other", "The CMD button, the check at launch and the debug window.", [
            self._row_panel(
                "CMD button", "What the CMD button opens: a console window inside the app, or a real terminal "
                "running the classic menu.",
                self._make_cmd_control()),
            # v0.16.17
            self._row_panel(
                "Scan on launch", "Check every playlist against Spotify / YouTube each time the app opens. "
                "Off, the last known numbers are shown until you run a Scan or Sync. Takes effect next launch.",
                self._make_launch_scan_control()),
            # v0.16.3
            self._row_panel(
                "Debug window",
                "Shows the app’s live log and printed output in a real console window"
                if ui_debug.uses_real_console() else
                "Shows the app’s live log and printed output in a window",
                self._make_debug_control()),
        ])
        col.addStretch(1)

        # ---- Download Settings (v0.23.2: one group, Download Settings)
        col = self._add_page(self.PAGE_DOWNLOADS)
        self._add_group(col, "download", "Download Settings",
                        "How songs are found and downloaded, for every playlist.", [
            # v0.17.7, Checklist item 58
            self._row_panel(
                "Prefer explicit versions", "When a song has both an explicit and a clean version on YouTube, "
                "download the explicit one. Off, the usual search decides. Applies to songs downloaded from now "
                "on; songs already downloaded are kept (fix one with an exception).",
                self._make_explicit_control()),
            self._make_cookies_panel(),    # v0.16.42, Checklist item 48
            self._make_download_panel(),   # v0.16.44, Checklist item 39
        ])
        # v0.25.0, Checklist item 65
        self._add_group(col, "clock", "Scheduled Sync",
                        "Sync every playlist once a day, even when the app is closed.", [
            self._make_schedule_panel(),
        ])
        col.addStretch(1)

        # ---- User Data & Installation
        col = self._add_page(self.PAGE_USER)
        self._add_group(col, "user_data", "User Data",
                        "Your keys, your Music folder, and a backup of everything you've set up.", [
            # v0.16.26, Checklist item 27 part 5
            self._row_panel(
                "Keyboard", "Change the keys for the Main Menu, the Edit page and the other menus. "
                "Press Tab anywhere to see the current keys.",
                self._make_remap_control()),
            self._make_folder_panel(),
            # v0.24.0, Checklist item 64
            self._row_panel(
                "Playlist files", "Each Sync saves a playlist file (.m3u8) in the playlist's folder, with the "
                "songs in the playlist's order, so other music players show the playlist as it is. Off, the "
                "app's playlist files are removed at each playlist's next Sync.",
                self._make_playlist_files_control()),
            self._make_data_panel(),   # v0.17.8, Checklist item 59; data folder v0.18.1
        ])
        self._add_group(col, "install", "User Installation",
                        "This copy of Mud Music Manager: its updates, and the libraries and programs it runs on.", [
            self._make_updates_panel(),
            self._make_libraries_panel(),
        ])
        col.addStretch(1)

        # ---- About (v0.17.1, Checklist item 52; its own page since v0.23.0)
        col = self._add_page(self.PAGE_ABOUT)
        col.addWidget(self._make_about_panel())
        # v0.27.0: the same Support window as the Main Menu's heart button
        self.support_btn = make_button("Support Mud Music Manager", "primary", large=True)
        self.support_btn.setToolTip("A note from the developer, and a link to his Ko-fi page")
        self.support_btn.clicked.connect(lambda: ui_dialogs.SupportDialog(self.window()).exec())
        col.addSpacing(6)
        col.addWidget(self.support_btn, 0, Qt.AlignLeft)
        col.addStretch(1)

        # v0.23.1: Project libraries and the components line are read in the
        # background each time Settings opens (refresh_installation()), not here.
        self.refresh()

    # ---- pages and groups (v0.23.0) -------------------------------------------
    def _add_page(self, key: str) -> QVBoxLayout:
        """A page in its own scroll area; returns the column to fill."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # The panels wrap their text, so this only shows if the sheet is ever too
        # narrow for a row's controls: they scroll into view instead of being cut off.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 8, 0)
        col.setSpacing(16)
        scroll.setWidget(host)
        smooth_scroll(scroll)  # v0.17.0
        self._page_index[key] = self.pages.addWidget(scroll)
        return col

    @staticmethod
    def _add_group(col: QVBoxLayout, icon: str, title: str, text: str, panels) -> None:
        """A titled group: its icon, name and a line about it, then its panels."""
        head = QHBoxLayout()
        head.setSpacing(12)
        head.setContentsMargins(4, 6, 0, 0)
        head.addWidget(GroupIcon(icon, 36), 0, Qt.AlignVCenter)
        words = QVBoxLayout()
        words.setSpacing(0)
        name = QLabel(title)
        name.setProperty("role", "h2")
        words.addWidget(name)
        sub = QLabel(text)
        sub.setProperty("role", "muted")
        sub.setWordWrap(True)
        words.addWidget(sub)
        head.addLayout(words, 1)
        col.addLayout(head)
        box = QVBoxLayout()
        box.setSpacing(12)
        for p in panels:
            box.addWidget(p)
        col.addLayout(box)
        col.addSpacing(30)  # v0.23.1: a clear gap before what follows (was 10)

    def show_page(self, key: str) -> None:
        if key not in self._page_index:
            return
        self.pages.setCurrentIndex(self._page_index[key])
        if key == self.PAGE_ABOUT:
            self.nav_ctl.clear_selection()
            self.about_ctl.set_current(key)
        else:
            self.about_ctl.clear_selection()
            self.nav_ctl.set_current(key)

    def current_page_key(self) -> str:
        return self.about_ctl.current() or self.nav_ctl.current() or self.PAGE_PREFERENCES

    # ---- Music folder ----------------------------------------------------------
    def _make_folder_panel(self) -> QWidget:
        folder = make_panel()
        fl = QVBoxLayout(folder)
        fl.setContentsMargins(22, 18, 22, 18)
        fl.setSpacing(8)
        fl.addWidget(CapsLabel("Music folder", muted=True))
        self.folder_label = ElidedLabel("", mode=Qt.ElideMiddle)
        fl.addWidget(self.folder_label)
        hint = QLabel("Change it from the main menu (Music Folder → Change).")
        hint.setProperty("role", "muted")
        fl.addWidget(hint)
        return folder

    # ---- Updates ---------------------------------------------------------------
    def _make_updates_panel(self) -> QWidget:
        upd = make_panel()
        ul = QHBoxLayout(upd)
        ul.setContentsMargins(22, 18, 22, 18)
        ucol = QVBoxLayout()
        ucol.setSpacing(2)
        ucol.addWidget(CapsLabel("Updates", muted=True))
        # v0.19.0: the app too; v0.28.0: a release carries the newest of everything
        self.update_label = QLabel("Check for a newer Mud Music Manager. Each release includes the newest "
                                   "Python, libraries, Deno and FFmpeg.")
        self.update_label.setTextFormat(Qt.RichText)  # v0.28.0: the Details link
        self.update_label.linkActivated.connect(lambda _href: self.updateInfoRequested.emit())
        self.update_label.setWordWrap(True)
        self.update_label.setProperty("role", "muted")
        ucol.addWidget(self.update_label)
        # v0.22.0 (Checklist item 60g): the packaged app's components and their versions
        self.components_label = QLabel("")
        self.components_label.setProperty("role", "faint")
        self.components_label.setWordWrap(True)
        self.components_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ucol.addWidget(self.components_label)
        self.components_msg = QLabel("")
        self.components_msg.setProperty("role", "muted")
        self.components_msg.setWordWrap(True)
        self.components_msg.hide()
        ucol.addWidget(self.components_msg)
        ul.addLayout(ucol, 1)
        ubtns = QVBoxLayout()
        ubtns.setSpacing(8)
        self.update_btn = make_button("Check For Updates")
        self.update_btn.clicked.connect(self.checkUpdatesRequested.emit)
        ubtns.addWidget(self.update_btn)
        # v0.28.0 (Checklist item 60h): Update & Restart MMM under it when there are
        # updates, with its note and progress bar (the same as the header's)
        self.restart_box = UpdateRestartBox()
        self.restart_btn = self.restart_box.button
        self.restart_btn.clicked.connect(self.restartRequested.emit)
        self.restart_box.hide()
        ubtns.addWidget(self.restart_box)
        self.builtin_btn = make_button("Use built-in versions", "ghost", compact=True)
        self.builtin_btn.setToolTip("Stop using the updated libraries / Deno / FFmpeg and go back to the ones this "
                                    "version came with (from the next launch)")
        self.builtin_btn.clicked.connect(self._use_builtin_components)
        ubtns.addWidget(self.builtin_btn)
        ubtns.addStretch(1)
        ul.addLayout(ubtns)
        self.components_label.setVisible(False)  # filled in when Settings opens (refresh())
        self.builtin_btn.setVisible(False)
        return upd

    # ---- Project libraries -----------------------------------------------------
    def _make_libraries_panel(self) -> QWidget:
        libs = make_panel()
        ll = QVBoxLayout(libs)
        ll.setContentsMargins(22, 18, 22, 18)
        ll.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(CapsLabel("Project libraries", muted=True))
        head.addStretch(1)
        refresh = make_button("Refresh", "ghost", compact=True)
        refresh.clicked.connect(lambda: self.refresh_installation(reread_tools=True))
        head.addWidget(refresh)
        ll.addLayout(head)
        sub = QLabel("Where each library from requirements.txt is installed on this computer, and the Deno and "
                     "FFmpeg programs the app uses — handy for checking their files if something behaves "
                     "inconsistently.")
        sub.setProperty("role", "muted")
        sub.setWordWrap(True)
        ll.addWidget(sub)
        self.libs_status = QLabel("Reading the installed libraries…")  # v0.23.1: until the first read ends
        self.libs_status.setProperty("role", "faint")
        self.libs_status.setWordWrap(True)
        ll.addWidget(self.libs_status)
        self.libs_box = QVBoxLayout()
        self.libs_box.setSpacing(8)
        ll.addLayout(self.libs_box)
        self.env_label = QLabel("")
        self.env_label.setProperty("role", "faint")
        self.env_label.setWordWrap(True)
        self.env_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # v0.23.0: its paths can't wrap (no spaces), so like the Data folder line it
        # never widens Settings; the end of a long one is cut off (full text in the tooltip).
        self.env_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.env_label.setMinimumWidth(1)
        ll.addWidget(self.env_label)
        return libs

    # ------------------------------------------------------------------
    def _row_panel(self, title: str, text: str, control: QWidget):
        p = make_panel()
        lay = QHBoxLayout(p)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(18)
        c = QVBoxLayout()
        c.setSpacing(2)
        c.addWidget(CapsLabel(title, muted=True))
        d = QLabel(text)
        d.setProperty("role", "muted")
        d.setWordWrap(True)
        c.addWidget(d)
        lay.addLayout(c, 1)
        lay.addWidget(control, 0, Qt.AlignVCenter)
        return p

    def _make_theme_control(self):
        self.theme_ctl = SegmentedControl(
            [(ui_settings.THEME_DARK, "Dark"), (ui_settings.THEME_LIGHT, "Light")],
            ui_settings.get("theme"))
        self.theme_ctl.changed.connect(self._theme_picked)
        return self.theme_ctl

    def _make_launch_scan_control(self):
        self.launch_scan_ctl = SegmentedControl([("on", "On"), ("off", "Off")], self._launch_scan_key())
        self.launch_scan_ctl.changed.connect(lambda k: ui_settings.set_value("scan_on_launch", k == "on"))
        return self.launch_scan_ctl

    @staticmethod
    def _launch_scan_key() -> str:
        return "on" if ui_settings.get("scan_on_launch") else "off"

    def _make_smooth_scroll_control(self):
        self.smooth_scroll_ctl = SegmentedControl([("on", "On"), ("off", "Off")], self._smooth_scroll_key())
        self.smooth_scroll_ctl.changed.connect(lambda k: ui_settings.set_value("smooth_scroll", k == "on"))
        return self.smooth_scroll_ctl

    @staticmethod
    def _smooth_scroll_key() -> str:
        return "on" if ui_settings.get("smooth_scroll") else "off"

    def _make_explicit_control(self):
        self.explicit_ctl = SegmentedControl([("on", "On"), ("off", "Off")], self._explicit_key())
        self.explicit_ctl.changed.connect(lambda k: ui_settings.set_value("prefer_explicit", k == "on"))
        return self.explicit_ctl

    @staticmethod
    def _explicit_key() -> str:
        return "on" if ui_settings.get("prefer_explicit") else "off"

    def _make_playlist_files_control(self):
        self.playlist_files_ctl = SegmentedControl([("on", "On"), ("off", "Off")], self._playlist_files_key())
        self.playlist_files_ctl.changed.connect(lambda k: ui_settings.set_value("playlist_files", k == "on"))
        return self.playlist_files_ctl

    @staticmethod
    def _playlist_files_key() -> str:
        return "off" if ui_settings.get("playlist_files") is False else "on"

    # ---- Scheduled sync (v0.25.0, Checklist item 65) -----------------------
    def _make_schedule_panel(self) -> QWidget:
        p = make_panel()
        lay = QVBoxLayout(p)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(18)
        words = QVBoxLayout()
        words.setSpacing(2)
        words.addWidget(CapsLabel("Scheduled sync", muted=True))
        d = QLabel("Every day at the time you choose, every playlist is synced, as Sync Now with all of them "
                   "ticked, and watched artists' new releases are added. It runs even when the app is closed "
                   "(a Windows scheduled task, while you're signed in to Windows); if the app is open, the app "
                   "runs it. A sync missed while the PC was off runs when it's next on.")
        d.setProperty("role", "muted")
        d.setWordWrap(True)
        words.addWidget(d)
        top.addLayout(words, 1)
        self.schedule_ctl = SegmentedControl([("on", "On"), ("off", "Off")], "off")
        self.schedule_ctl.changed.connect(lambda k: self._schedule_changed(enabled=k == "on"))
        top.addWidget(self.schedule_ctl, 0, Qt.AlignVCenter)
        lay.addLayout(top)

        self.schedule_row = QWidget()
        row = QHBoxLayout(self.schedule_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(CapsLabel("Time", muted=True), 0, Qt.AlignVCenter)
        self.schedule_time = QTimeEdit()
        self.schedule_time.setDisplayFormat("HH:mm")
        self.schedule_time.setToolTip("When the sync runs each day")
        self.schedule_time.editingFinished.connect(self._schedule_time_edited)
        row.addWidget(self.schedule_time, 0, Qt.AlignVCenter)
        row.addSpacing(18)
        row.addWidget(CapsLabel("Notification", muted=True), 0, Qt.AlignVCenter)
        self.schedule_notify_ctl = SegmentedControl([("on", "On"), ("off", "Off")], "on")
        self.schedule_notify_ctl.setToolTip("A Windows notification when the scheduled sync finishes")
        self.schedule_notify_ctl.changed.connect(lambda k: self._schedule_changed(notify=k == "on", task=False))
        row.addWidget(self.schedule_notify_ctl, 0, Qt.AlignVCenter)
        row.addStretch(1)
        self.schedule_try_btn = make_button("Try it now", "ghost", compact=True)
        self.schedule_try_btn.setToolTip("Run the scheduled sync now in this window, with its notification")
        self.schedule_try_btn.clicked.connect(self._schedule_try)
        row.addWidget(self.schedule_try_btn, 0, Qt.AlignVCenter)
        lay.addWidget(self.schedule_row)

        self.schedule_status = QLabel("")
        self.schedule_status.setProperty("role", "muted")
        self.schedule_status.setWordWrap(True)
        lay.addWidget(self.schedule_status)
        self._schedule_error = None
        self._sync_schedule_controls()
        return p

    def _sync_schedule_controls(self) -> None:
        s = scheduled_sync.get_setting()
        ok = scheduled_sync.supported()
        self.schedule_ctl.set_current("on" if s["enabled"] else "off")
        self.schedule_ctl.setEnabled(ok)
        h, m = (int(x) for x in s["time"].split(":"))
        self.schedule_time.blockSignals(True)
        self.schedule_time.setTime(QTime(h, m))
        self.schedule_time.blockSignals(False)
        self.schedule_notify_ctl.set_current("on" if s["notify"] else "off")
        self.schedule_row.setVisible(ok and s["enabled"])
        if not ok:
            self.schedule_status.setText("Scheduled sync is only available on Windows.")
            _set_role(self.schedule_status, "muted")
        elif self._schedule_error:
            self.schedule_status.setText(f"Couldn't update the Windows scheduled task: {self._schedule_error}")
            _set_role(self.schedule_status, "bad")
        else:
            lead = f"On: {scheduled_sync.describe(s)}. " if s["enabled"] else ""
            self.schedule_status.setText(lead + scheduled_sync.last_result_text())
            _set_role(self.schedule_status, "muted")

    def _schedule_changed(self, task: bool = True, **changes) -> None:
        scheduled_sync.set_setting(**changes)
        if task:
            self._schedule_error = scheduled_sync.apply()
        self._sync_schedule_controls()

    def _schedule_time_edited(self) -> None:
        at = self.schedule_time.time().toString("HH:mm")
        if at != scheduled_sync.get_setting()["time"]:
            self._schedule_changed(time=at)

    def _schedule_try(self) -> None:
        window = getattr(self.ctx, "window", None)
        if window is not None:
            window.run_scheduled_sync()

    # ---- Download quality (v0.16.44) -------------------------------------
    _DOWNLOAD_HINTS = {
        "bitrate": "Used when the audio has to be converted to AAC. Higher keeps more of the source but makes "
                   "bigger files; it can’t add quality the source doesn’t have.",
        "speed_limit": "How fast each song may download.",
        "pause": "A random wait before each download. May help when YouTube refuses downloads on long syncs; "
                 "makes syncs slower.",
    }

    def _make_download_panel(self) -> QWidget:
        p = make_panel()
        lay = QVBoxLayout(p)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(CapsLabel("Download quality", muted=True))
        d = QLabel("How songs and their cover art are downloaded, for every playlist. Changes apply to songs downloaded "
                   "from now on; songs already downloaded are kept as they are (in their own format).")
        d.setProperty("role", "muted")
        d.setWordWrap(True)
        lay.addWidget(d)

        self.download_btns = {}
        self.download_hints = {}
        for key in download_settings.OPTIONS:
            row = QHBoxLayout()
            row.setSpacing(18)
            text = QVBoxLayout()
            text.setSpacing(0)
            name = QLabel(download_settings.NAMES[key])
            f = name.font()
            f.setBold(True)
            name.setFont(f)
            text.addWidget(name)
            hint = QLabel("")
            hint.setProperty("role", "muted")
            hint.setWordWrap(True)
            text.addWidget(hint)
            row.addLayout(text, 1)
            btn = make_button("")
            btn.setMinimumWidth(190)
            btn.clicked.connect(lambda _c=False, k=key: self._pick_download_option(k))
            row.addWidget(btn, 0, Qt.AlignVCenter)
            lay.addLayout(row)
            self.download_btns[key] = btn
            self.download_hints[key] = hint

        foot = QHBoxLayout()
        foot.setSpacing(12)
        self.download_status = QLabel("")
        self.download_status.setWordWrap(True)
        foot.addWidget(self.download_status, 1, Qt.AlignVCenter)
        self.download_reset_btn = make_button("Reset to defaults")
        self.download_reset_btn.setToolTip("Go back to the default download settings")
        self.download_reset_btn.clicked.connect(self._reset_download_settings)
        foot.addWidget(self.download_reset_btn, 0, Qt.AlignVCenter)
        lay.addLayout(foot)
        self._sync_download_controls()
        return p

    def _sync_download_controls(self) -> None:
        s = download_settings.get_setting()
        is_m4a = s["file_format"] == "m4a"
        for key, btn in self.download_btns.items():
            btn.setText(f"{download_settings.label(key, s[key])}  ▾")
            if key in ("file_format", "source", "cover_format"):
                hint = download_settings.description(key, s[key])
            else:
                hint = self._DOWNLOAD_HINTS[key]
            if key == "source" and not is_m4a:
                hint = (f"Only matters for .m4a: a {download_settings.file_extension(s)} file is always "
                        f"converted from the stream chosen here.")
            if key == "bitrate":
                if not download_settings.bitrate_applies(s):
                    hint = f"Not used for {download_settings.file_extension(s)}: it is lossless."
                elif is_m4a and s["source"] == "aac":
                    hint = ("Only used when a video has no AAC stream and its audio has to be converted. "
                            "AAC is copied as it is.")
            btn.setEnabled(key != "bitrate" or download_settings.bitrate_applies(s))
            self.download_hints[key].setText(hint)
        changed = download_settings.changed_keys(s)
        if changed:
            names = ", ".join(download_settings.NAMES[k] for k in changed)
            self.download_status.setText(f"Changed from the defaults: {names}.")
            _set_role(self.download_status, "warn")
        else:
            self.download_status.setText("Using the default settings.")
            _set_role(self.download_status, "muted")
        self.download_reset_btn.setEnabled(bool(changed))

    def _pick_download_option(self, key: str) -> None:
        btn = self.download_btns[key]
        menu = QMenu(self)
        current = download_settings.get_setting()[key]
        default = download_settings.DEFAULTS[key]
        for value, label, _desc in download_settings.OPTIONS[key]:
            act = menu.addAction(f"{label}  (default)" if value == default else label)
            act.setCheckable(True)
            act.setChecked(value == current)
            act.triggered.connect(lambda _c=False, v=value: self._download_option_picked(key, v))
        menu.exec(btn.mapToGlobal(QPoint(0, btn.height())))

    def _download_option_picked(self, key: str, value: str) -> None:
        download_settings.set_setting(**{key: value})
        self._sync_download_controls()

    def _reset_download_settings(self) -> None:
        download_settings.reset()
        self._sync_download_controls()

    # ---- YouTube sign-in (v0.16.42) -------------------------------------
    def _make_cookies_panel(self) -> QWidget:
        p = make_panel()
        lay = QVBoxLayout(p)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(CapsLabel("YouTube sign-in", muted=True))
        d = QLabel("Age-restricted videos, and downloads YouTube refuses with its “not a bot” check, need a "
                   "signed-in YouTube account. Downloads stay anonymous: only those refused downloads are tried "
                   "once more with the YouTube cookies of a browser you're signed in on, or of a cookies.txt "
                   "file. Your browser and the file are only read, never changed.")
        d.setProperty("role", "muted")
        d.setWordWrap(True)
        lay.addWidget(d)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.cookie_mode_ctl = SegmentedControl(
            [(youtube_cookies.MODE_OFF, "Off"), (youtube_cookies.MODE_BROWSER, "Browser"),
             (youtube_cookies.MODE_FILE, "cookies.txt")],
            youtube_cookies.get_setting()["mode"])
        self.cookie_mode_ctl.changed.connect(self._cookie_mode_picked)
        row.addWidget(self.cookie_mode_ctl, 0, Qt.AlignVCenter)
        self.browser_btn = make_button("")
        self.browser_btn.setToolTip("Which browser's YouTube cookies to use (its default profile)")
        self.browser_btn.clicked.connect(self._pick_browser)
        row.addWidget(self.browser_btn, 0, Qt.AlignVCenter)
        self.cookie_file_label = ElidedLabel("", mode=Qt.ElideMiddle)
        row.addWidget(self.cookie_file_label, 1, Qt.AlignVCenter)
        self.cookie_file_btn = make_button("Choose file…")
        self.cookie_file_btn.clicked.connect(self._pick_cookie_file)
        row.addWidget(self.cookie_file_btn, 0, Qt.AlignVCenter)
        row.addStretch(1)
        self.cookie_test_btn = make_button("Test")
        self.cookie_test_btn.setToolTip("Read the cookies now and check they hold a signed-in YouTube account")
        self.cookie_test_btn.clicked.connect(self._test_cookies)
        row.addWidget(self.cookie_test_btn, 0, Qt.AlignVCenter)
        lay.addLayout(row)

        self.cookie_note = QLabel("")
        self.cookie_note.setProperty("role", "warn")
        self.cookie_note.setWordWrap(True)
        lay.addWidget(self.cookie_note)
        self.cookie_result = QLabel("")
        self.cookie_result.setWordWrap(True)
        self.cookie_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.cookie_result.hide()
        lay.addWidget(self.cookie_result)
        self._cookie_testing = False
        self._sync_cookie_controls()
        return p

    def _sync_cookie_controls(self) -> None:
        s = youtube_cookies.get_setting()
        mode = s["mode"]
        self.cookie_mode_ctl.set_current(mode)
        self.browser_btn.setText(f"{youtube_cookies.browser_label(s['browser'])}  ▾")
        self.browser_btn.setVisible(mode == youtube_cookies.MODE_BROWSER)
        file_mode = mode == youtube_cookies.MODE_FILE
        self.cookie_file_label.setText(s["file"] or "No file chosen")
        self.cookie_file_label.setToolTip(s["file"])
        self.cookie_file_label.setVisible(file_mode)
        self.cookie_file_btn.setVisible(file_mode)
        self.cookie_test_btn.setVisible(mode != youtube_cookies.MODE_OFF)
        self.cookie_test_btn.setEnabled(youtube_cookies.is_on(s) and not self._cookie_testing)
        if mode == youtube_cookies.MODE_BROWSER:
            note = youtube_cookies.chromium_note(s["browser"])
            _set_role(self.cookie_note, "warn")
        elif file_mode:
            note = ("Export it in Netscape format from a browser signed in to YouTube (e.g. with a "
                    "“Get cookies.txt LOCALLY” extension). The file is only read.")
            _set_role(self.cookie_note, "muted")
        else:
            note = ""
        self.cookie_note.setText(note)
        self.cookie_note.setVisible(bool(note))

    def _cookie_setting_changed(self, **change) -> None:
        youtube_cookies.set_setting(**change)
        self.cookie_result.setText("")  # an earlier Test's answer no longer applies
        self.cookie_result.hide()
        self._sync_cookie_controls()

    def _cookie_mode_picked(self, mode: str) -> None:
        self._cookie_setting_changed(mode=mode)
        if mode == youtube_cookies.MODE_FILE and not youtube_cookies.get_setting()["file"]:
            self._pick_cookie_file()

    def _pick_browser(self) -> None:
        menu = QMenu(self)
        current = youtube_cookies.get_setting()["browser"]
        for name, label in youtube_cookies.browsers():
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(name == current)
            act.triggered.connect(lambda _c=False, n=name: self._cookie_setting_changed(browser=n))
        menu.exec(self.browser_btn.mapToGlobal(QPoint(0, self.browser_btn.height())))

    def _pick_cookie_file(self) -> None:
        current = youtube_cookies.get_setting()["file"]
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a cookies.txt file", os.path.dirname(current) if current else "",
            "Cookie files (*.txt);;All files (*)")
        if path:
            self._cookie_setting_changed(file=os.path.normpath(path))

    def _test_cookies(self) -> None:
        backend = getattr(self.ctx, "backend", None)
        setting = youtube_cookies.get_setting()
        if backend is None or self._cookie_testing or not youtube_cookies.is_on(setting):
            return
        self._cookie_testing = True
        self.cookie_result.setText(f"Reading {youtube_cookies.describe(setting)}…")
        _set_role(self.cookie_result, "muted")
        self.cookie_result.show()
        self.cookie_test_btn.setEnabled(False)
        sig = youtube_cookies.signature(setting)
        thread = backend.run(lambda _t: (sig, *youtube_cookies.test(setting)), heavy=False)
        # Bound methods, so the answer is handled on the GUI thread.
        thread.resultReady.connect(self._on_cookie_test)
        thread.failed.connect(self._on_cookie_test_failed)

    def _on_cookie_test_failed(self, message: str) -> None:
        self._on_cookie_test((youtube_cookies.signature(), False, f"Test failed: {message}"))

    def _on_cookie_test(self, result) -> None:
        self._cookie_testing = False
        sig, ok, message = result
        if sig != youtube_cookies.signature():  # the setting changed meanwhile
            self._sync_cookie_controls()
            return
        self.cookie_result.setText(message)
        _set_role(self.cookie_result, "ok" if ok else "bad")
        self.cookie_result.show()
        self._sync_cookie_controls()

    def _make_cmd_control(self):
        self.cmd_ctl = SegmentedControl(
            [(ui_settings.CMD_WINDOW, "App window"), (ui_settings.CMD_TERMINAL, "OS terminal")],
            ui_settings.get("cmd_mode"))
        self.cmd_ctl.changed.connect(lambda k: ui_settings.set_value("cmd_mode", k))
        return self.cmd_ctl

    def _make_debug_control(self):
        self.debug_btn = make_button("Open Debug Window")
        self.debug_btn.clicked.connect(self._toggle_debug)
        self._debug_timer = QTimer(self)  # notices the console being closed from its own X button
        self._debug_timer.setInterval(1000)
        self._debug_timer.timeout.connect(self._sync_debug_button)
        return self.debug_btn

    def _make_remap_control(self):
        self.remap_btn = make_button("Remap keyboard controls")
        self.remap_btn.clicked.connect(self._open_remap)
        return self.remap_btn

    # ---- Your data (v0.17.8) ------------------------------------------------
    def _make_data_panel(self) -> QWidget:
        """Export / Import (v0.17.8) on top; under them (v0.18.1) the folder the
        app keeps the user's data in (app_paths.DATA_DIR)."""
        p = make_panel()
        outer = QVBoxLayout(p)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(14)
        top = QHBoxLayout()
        top.setSpacing(18)
        c = QVBoxLayout()
        c.setSpacing(2)
        c.addWidget(CapsLabel("Your data", muted=True))
        d = QLabel("Save your playlists, albums, artists, exceptions, settings and playlist pictures to "
                   "one backup file, to set up Mud Music Manager on another computer (or after reinstalling) "
                   "with Import. Your music files aren't included: Sync downloads them again.")
        d.setProperty("role", "muted")
        d.setWordWrap(True)
        c.addWidget(d)
        top.addLayout(c, 1)
        top.addWidget(self._make_data_control(), 0, Qt.AlignVCenter)
        outer.addLayout(top)

        f = QVBoxLayout()
        f.setSpacing(2)
        f.addWidget(CapsLabel("Data folder", muted=True))
        # v0.18.2: a plain selectable label (ElidedLabel paints its own text, so
        # it can't be selected): drag to select, or right-click → Select All / Copy.
        self.data_dir_label = QLabel(app_paths.DATA_DIR)
        self.data_dir_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.data_dir_label.setCursor(Qt.IBeamCursor)
        # A long path is cut off at the panel's edge rather than widening Settings;
        # Select All + Copy still copies all of it.
        self.data_dir_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.data_dir_label.setMinimumWidth(1)
        self.data_dir_label.setToolTip(f"{app_paths.DATA_DIR}\n(select it, or right-click → Copy)")
        f.addWidget(self.data_dir_label)
        where = ("Kept in the program's own folder (portable.txt is next to the program)."
                 if app_paths.is_portable() else
                 "Where the app keeps your playlists, exceptions, settings, pictures, logs and backups.")
        hint = QLabel(where)
        hint.setProperty("role", "faint")
        hint.setWordWrap(True)
        f.addWidget(hint)
        outer.addLayout(f)
        return p

    def _make_data_control(self):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.export_btn = make_button("Export…")
        self.export_btn.setToolTip("Save your data to a backup file")
        self.export_btn.clicked.connect(self._export_data)
        self.import_btn = make_button("Import…")
        self.import_btn.setToolTip("Replace this computer's data with a backup's")
        self.import_btn.clicked.connect(self._import_data)
        row.addWidget(self.export_btn)
        row.addWidget(self.import_btn)
        return box

    @staticmethod
    def _documents_dir() -> str:
        return QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or os.path.expanduser("~")

    def _export_data(self) -> None:
        start = os.path.join(self._documents_dir(), user_data.default_filename())
        path, _ = QFileDialog.getSaveFileName(self, "Export your data", start,
                                              f"Mud Music Manager backup (*{user_data.FILE_EXTENSION})")
        if not path:
            return
        path = os.path.normpath(path)
        if not path.lower().endswith(user_data.FILE_EXTENSION):
            path += user_data.FILE_EXTENSION
        try:
            counts = user_data.export_data(path, getattr(self.ctx, "version", ""))
        except user_data.BackupError as e:
            ui_dialogs.warn(self.window(), "Export failed", str(e))
            return
        ui_dialogs.info(
            self.window(), "Data exported",
            f"Saved to:\n{path}\n\n{counts.describe()}, and your settings.\n\n"
            "To use it on another computer, install Mud Music Manager there, then choose Settings → User Data & "
            "Installation → Your data → Import… and pick this file. Your music files aren't in the backup: Sync downloads them again, "
            "or copy your Music folder across too.")

    def _import_data(self) -> None:
        backend = self.ctx.backend
        if backend.heavy_running:
            ui_dialogs.warn(self.window(), "Import your data",
                            "Something is running (a Sync, Check Integrity or a save). Import once it has finished.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import your data", self._documents_dir(),
            f"Mud Music Manager backup (*{user_data.FILE_EXTENSION});;All files (*)")
        if not path:
            return
        try:
            backup = user_data.read_backup(os.path.normpath(path))
        except user_data.BackupError as e:
            ui_dialogs.warn(self.window(), "Can't import that file", str(e))
            return

        win = getattr(self.ctx, "window", None)
        edit_open = win is not None and win.edit_page_showing()
        dlg = ui_dialogs.ImportDataDialog(
            self.window(), backup, user_data.library_path_after_import(backup),
            edit_open=edit_open, edit_unsaved=edit_open and win.edit_page.has_unsaved())
        if dlg.exec() != dlg.Accepted:
            return

        busy = ui_dialogs.BusyDialog(self.window(), "Importing your data", "Waiting for the background scan to stop…")
        busy.open()

        def run():
            # On the GUI thread, once the launch-time scan (if any) has stopped.
            if backend.heavy_running:  # started meanwhile
                busy.finish()
                ui_dialogs.warn(self.window(), "Import your data",
                                "Something else started meanwhile. Nothing was imported.")
                return
            busy.set_message("Importing…")
            try:
                report = user_data.import_backup(backup, getattr(self.ctx, "version", ""))
            except user_data.BackupError as e:
                busy.finish()
                if win is not None:
                    win.data_imported()  # show whatever is on disk now
                self.refresh()
                ui_dialogs.warn(self.window(), "Import failed", str(e))
                return
            busy.finish()
            if win is not None:
                win.data_imported()
            self.refresh()
            ui_dialogs.info(self.window(), "Data imported", self._import_message(backup, report))

        backend.when_refresh_stopped(run)

    @staticmethod
    def _import_message(backup, report) -> str:
        lines = [f"Imported {report.counts.describe()}, and your settings, from the backup made "
                 f"{backup.created_text()}."]
        if report.library_path:
            lines.append(f"Music folder: {report.library_path}")
        else:
            lines.append("No Music folder is set yet. Choose one on the Main Menu (Music Folder → Change), "
                         "then Sync to download your songs.")
        if report.missing_custom_folders:
            names = ", ".join(report.missing_custom_folders)
            lines.append(f"Linked to a folder that isn't on this computer: {names}. A Sync puts their songs at "
                         f"that same path if it can; to keep them in the Music folder instead, remove and add "
                         f"them again.")
        if report.missing_cookie_file:
            lines.append(f"YouTube sign-in uses a cookies.txt file that isn't on this computer "
                         f"({report.missing_cookie_file}). Choose it again under Download Settings → "
                         f"YouTube sign-in.")
        lines.append(f"Your data from before the import is saved in:\n{report.safety_copy}")
        return "\n\n".join(lines)

    def _open_remap(self) -> None:
        keys = getattr(self.ctx, "keys", None)
        if keys is None:
            return
        ui_remap.RemapDialog(self.window(), keys).exec()

    def _toggle_debug(self) -> None:
        if ui_debug.is_open():
            ui_debug.close_debug()
        else:
            note = ui_debug.open_debug(self)
            if note:
                ui_dialogs.warn(self, "Debug Window", note)
        self._sync_debug_button()

    def _sync_debug_button(self) -> None:
        self.debug_btn.setText("Close Debug Window" if ui_debug.is_open() else "Open Debug Window")

    def showEvent(self, e):
        self._sync_debug_button()
        self._debug_timer.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self._debug_timer.stop()
        super().hideEvent(e)

    def key_default(self):
        """Keyboard (v0.16.24): the first key lands on the tab of the page
        showing (v0.23.0; it was the theme choice, the first setting)."""
        key = self.current_page_key()
        ctl = self.about_ctl if key == self.PAGE_ABOUT else self.nav_ctl
        return ctl._buttons.get(key)

    def _theme_picked(self, name: str) -> None:
        self.ctx.apply_theme(name)
        self.themeChanged.emit(name)

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        lib = config_manager.get_library_path(self.ctx.config)
        self.folder_label.setText(lib or "(not set)")
        self.folder_label.setToolTip(lib or "")
        self.theme_ctl.set_current(ui_settings.get("theme"))
        self.launch_scan_ctl.set_current(self._launch_scan_key())
        self.smooth_scroll_ctl.set_current(self._smooth_scroll_key())
        self.explicit_ctl.set_current(self._explicit_key())
        self.playlist_files_ctl.set_current(self._playlist_files_key())
        self.cmd_ctl.set_current(ui_settings.get("cmd_mode"))
        self._sync_download_controls()
        self._sync_cookie_controls()
        self._sync_schedule_controls()  # v0.25.0
        self._sync_debug_button()

    def set_update_state(self, text: str, restart: bool) -> None:
        """v0.28.0: the update check's line and Update & Restart MMM (the
        same as the header shows; the version pill there opens the details)."""
        link = ui_theme.palette().link
        self.update_label.setText(f'{html.escape(text)} &nbsp;<a href="details" style="color:{link}">Details</a>')
        self.restart_box.setVisible(restart)

    # ---- Components and Project libraries, read in the background (v0.23.1) -------
    # Reading them took ~0.6 s on the GUI thread every time Settings opened
    # (package metadata ~0.45 s; deno / ffmpeg run for their versions ~0.1 s),
    # so the app froze before the sheet appeared. Now Settings opens at once
    # and the two panels fill in when the read finishes.
    def refresh_installation(self, reread_tools: bool = False) -> None:
        """Read the components line and the Project libraries list off the GUI
        thread, then show them. reread_tools: run deno / ffmpeg again for the
        libraries list (Refresh) instead of using this session's answer."""
        backend = getattr(self.ctx, "backend", None)
        if backend is None:  # no worker threads (e.g. a test): read here
            self._show_installation(self._read_installation(reread_tools))
            return
        if self._install_reading:
            self._install_again = True  # read once more when this one ends
            self._install_reread = self._install_reread or reread_tools
            return
        self._install_reading = True
        thread = backend.run(lambda _t: self._read_installation(reread_tools), heavy=False)
        # Bound methods, so the answer is handled on the GUI thread.
        thread.resultReady.connect(self._on_installation_read)
        thread.failed.connect(self._on_installation_failed)

    @staticmethod
    def _read_installation(reread_tools: bool) -> dict:
        """Runs on a worker thread: no Qt here."""
        on = component_updates.enabled()
        lines = []
        if on:
            try:
                lines = component_updates.describe()
            except Exception as e:
                lines = [f"Couldn't read the component versions ({type(e).__name__})."]
        return {
            "components_on": on,
            "components": lines,
            "state": component_updates.load_state() if on else {},
            "libs": ui_libraries.describe_all(include_tools=False) + ui_libraries.describe_tools(refresh=reread_tools),
            "env": ui_libraries.python_environment(),
        }

    def _on_installation_read(self, result: dict) -> None:
        self._install_reading = False
        self._show_installation(result)
        self._read_again_if_asked()

    def _on_installation_failed(self, message: str) -> None:
        self._install_reading = False
        if self._libs_shown is None:
            self.libs_status.setText(f"Couldn't read the installed libraries ({message}).")
            _set_role(self.libs_status, "bad")
        self._read_again_if_asked()

    def _read_again_if_asked(self) -> None:
        if self._install_again:
            reread = self._install_reread
            self._install_again = self._install_reread = False
            self.refresh_installation(reread_tools=reread)

    def _show_installation(self, r: dict) -> None:
        # Components (v0.22.0, Checklist item 60g): the versions of yt-dlp /
        # yt-dlp-ejs / Deno / FFmpeg in use and where they come from.
        on = r["components_on"]
        self.components_label.setVisible(on)
        self.builtin_btn.setVisible(on)
        if on:
            self.components_label.setText("Components: " + " · ".join(r["components"]))
            # v0.22.2: running from source the "built-in" versions are the PC's own copies
            self.builtin_btn.setText("Use built-in versions" if app_paths.FROZEN else "Use this PC's versions")
            state = r["state"]
            self.builtin_btn.setEnabled(bool(state.get("active") or state.get("staged")) and not state.get("reset"))
        # Project libraries (v0.22.3: ending with Deno and FFmpeg). The rows are
        # only rebuilt when something changed, so reopening Settings doesn't flicker.
        self.libs_status.hide()
        if r["libs"] != self._libs_shown:
            self._libs_shown = r["libs"]
            while self.libs_box.count():
                item = self.libs_box.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            for info in r["libs"]:
                self.libs_box.addWidget(self._library_row(info))
        env = r["env"]
        self.env_label.setText(
            f"Python {env['version']}  ·  {env['executable']}\nPackages folder: {env['site_packages']}")
        self.env_label.setToolTip(self.env_label.text())

    def set_components_message(self, text: str) -> None:
        self.components_msg.setText(text)
        self.components_msg.setVisible(bool(text))

    def _use_builtin_components(self) -> None:
        label = self.builtin_btn.text()
        which = ("the library, Deno and FFmpeg versions this copy of Mud Music Manager came with"
                 if app_paths.FROZEN else "the Deno and FFmpeg installed on this PC")
        if not ui_dialogs.ask_yes_no(
                self.window(), label,
                f"Go back to {which}? It takes effect the next time the app starts. Newer versions will be "
                "looked for again later.",
                yes=label, no="Cancel"):
            return
        component_updates.reset_to_built_in()
        self.builtin_btn.setEnabled(False)  # the read below confirms it
        self.refresh_installation()
        self.set_components_message(f"{which[0].upper()}{which[1:]} will be used from the next launch.")

    def _library_row(self, info) -> QWidget:
        box = make_panel("inputbox")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(14, 10, 12, 10)
        lay.setSpacing(12)
        col = QVBoxLayout()
        col.setSpacing(0)
        top = QHBoxLayout()
        top.setSpacing(10)
        name = QLabel(info.name)
        f = name.font()
        f.setBold(True)
        name.setFont(f)
        top.addWidget(name)
        ver = QLabel(f"v{info.version}" if info.installed else "not installed")
        ver.setProperty("role", "muted" if info.installed else "bad")
        top.addWidget(ver)
        top.addStretch(1)
        col.addLayout(top)
        path = ElidedLabel(info.path or "—", mode=Qt.ElideMiddle)
        path.setProperty("role", "faint")
        path.setToolTip(info.path or "")
        col.addWidget(path)
        lay.addLayout(col, 1)
        if info.installed and info.path:
            copy = make_button("Copy path", compact=True)
            copy.clicked.connect(lambda _c=False, p=info.path: QGuiApplication.clipboard().setText(p))
            openb = make_button("Open folder", compact=True)
            openb.clicked.connect(lambda _c=False, p=info.path: self._open_folder(p))
            lay.addWidget(copy)
            lay.addWidget(openb)
        return box

    # ---- About (v0.17.1) ---------------------------------------------------
    def _make_about_panel(self) -> QWidget:
        p = make_panel()
        lay = QHBoxLayout(p)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(18)
        lay.addWidget(AppLogo(84), 0, Qt.AlignTop)
        c = QVBoxLayout()
        c.setSpacing(4)
        c.addWidget(CapsLabel("About", muted=True))
        name = QLabel(f"{app_info.APP_NAME}™")
        f = name.font()
        f.setBold(True)
        f.setPointSizeF(12)
        name.setFont(f)
        c.addWidget(name)
        ver = QLabel(f"Version {getattr(self.ctx, 'version', '')}")
        ver.setProperty("role", "muted")
        c.addWidget(ver)
        owner = QLabel(f"{app_info.COPYRIGHT}\n{app_info.TRADEMARK_NOTE}")
        owner.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # v0.22.1: wraps; unwrapped this line (835 px) forced every Settings panel
        # wider than the overlay, so their right ends were cut off.
        owner.setWordWrap(True)
        c.addWidget(owner)
        warranty = QLabel(app_info.WARRANTY_NOTE)  # v0.17.5: the GPL's no-warranty notice
        warranty.setProperty("role", "faint")
        warranty.setWordWrap(True)
        c.addWidget(warranty)
        note = QLabel(app_info.PLATFORM_DISCLAIMER)
        note.setProperty("role", "faint")
        note.setWordWrap(True)
        c.addWidget(note)
        btns = QHBoxLayout()
        btns.setSpacing(10)
        lic = make_button("License", compact=True)
        lic.clicked.connect(lambda: self._show_text("License", app_info.LICENSE_PATH))
        third = make_button("Third-party notices", compact=True)
        third.clicked.connect(lambda: self._show_text("Third-party notices", app_info.THIRD_PARTY_PATH))
        btns.addWidget(lic)
        btns.addWidget(third)
        btns.addStretch(1)
        c.addSpacing(4)
        c.addLayout(btns)
        lay.addLayout(c, 1)
        return p

    def _show_text(self, title: str, path: str) -> None:
        ui_dialogs.TextViewerDialog(self.window(), title, app_info.read_text(path),
                                    subtitle=app_info.APP_NAME).exec()

    @staticmethod
    def _open_folder(path: str) -> None:
        target = path if os.path.isdir(path) else os.path.dirname(path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))
