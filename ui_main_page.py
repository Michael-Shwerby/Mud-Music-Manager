"""
Mud Music Manager - Main Menu Screen (v0.16.0)

"Local Sync Console": the synced-playlist list on the left (reorder,
expand, edit, add, remove) and Actions / Music Folder / Status on the
right, with the Sync Progress panel along the bottom that only appears
while something is running.

Sync Now / Scan / Check Integrity all work the same way: click one and
a checkbox appears on every playlist (all unticked, with a button to
tick/untick them all), then confirm to run it on just the ticked ones.

v0.16.20 (Checklist item 27, part 1): keyboard control. W/A/S/D or the
arrow keys move a highlight between the buttons and playlists, Space
presses / expands / ticks, and single keys run the actions (keymap in
ui_keys.py). Every key presses the same button a mouse click would, so
busy / disabled states behave the same either way.

v0.16.21: Enter on a highlighted playlist opens its position box for
typing a new position; Esc there puts the old number back.

v0.16.22: Shift+W / Shift+S (Shift+Up / Shift+Down) move the highlighted
playlist one place up / down.

v0.16.28 (Checklist item 29): clicking a playlist (its row or its
chevron) also moves the keyboard highlight to it, so its keys work
straight away, e.g. click it and press E to open its Edit page.

v0.16.29 (Checklist item 30): Expand all / Collapse all buttons above the
list (keys O / Shift+O) open or close every playlist's details at once.

v0.16.30 (Checklist item 31): a search bar on the left of that row shows
only the playlists whose name contains every word typed (keys Ctrl+F or
/ to type in it). The list, the keys and Check all / Expand all work on
the playlists shown; ticks on hidden ones are kept.

v0.16.32: clearing the search scrolls the full list to the playlist the
keyboard selector is on (the one clicked, or picked with Enter / W / S).

v0.16.33 (Checklist item 34): while a playlist is being added, + Add
becomes "Queue Another Playlist". Queued playlists are added and synced
one after another. The progress panel's Queue button opens a list above
the bar (current, waiting with an ✕ each, done / failed); Cancel becomes
Cancel all while some are waiting.

v0.16.34 (Checklist item 35): slimmer playlist bars (102 -> 74 px when
closed). Only the empty space shrinks: smaller top / bottom margins,
shorter ▲ / ▼ buttons and no padding around the position number; every
text, arrow, thumbnail, badge and pill keeps its size.

v0.16.35 (Checklist item 36): a left click selects a playlist without
expanding it; a click on the selected one, a double click or a right
click opens / closes it.

v0.16.36 (Checklist item 37): Sync Now shows the queue list too (Queue
button, key L). Its ✕ on the playlist being synced stops that playlist
only, after the current download; the rest carry on. Cancel reads Cancel
all while playlists are waiting.

v0.16.49 (Checklist item 41a follow-up): albums are a separate list under
an "Albums & discographies" heading, below every playlist. Positions keep
one running count; ▲ / ▼, the position box and Shift+W / Shift+S move a
bar only within its own list.

v0.17.0: smoother motion. A bar's details slide down from under it at
their full size while its arrow turns and its open colours blend in (one
motion curve, ui_widgets.Drawer / DrawerSlide); artist bars do the same.
The Sync Progress panel rises from the bottom and its Queue list rises
from behind the progress line. A bar's arrow fades in on hover.

v0.27.0: a Support button (a lit heart, ui_widgets.HeartButton) at the
bottom right opens ui_dialogs.SupportDialog (Michael's note, Ko-fi link).
"""

import os
from typing import Dict, List, Optional

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

import config_manager
import ui_backend
import ui_dialogs
import ui_keys
import ui_settings
import artist_watch
import logger
from ui_widgets import (
    CapsLabel, ElidedLabel, HeartButton, Spinner, animations_on, make_button, make_panel, make_value_anim,
    motion_curve, run_value_anim, scroll_area_to, smooth_scroll, smooth_scrolling_on, ANIM_OPEN_MS,
)

ANIM_REORDER_MS = 220  # v0.17.0: a reordered bar sliding to its new place

from ui_main_rows import artist_key, ArtistRow, PlaylistRow, relative_time, _park_focus, _repolish
from ui_sync_panel import bar_weights, QueuedAdd, QueuedSync, SyncPanel
from ui_main_runs import MainPageRunsMixin
from ui_main_keys import MainPageKeysMixin


class MainPage(MainPageRunsMixin, MainPageKeysMixin, QWidget):
    openEdit = Signal(dict)
    openResults = Signal(str, list)   # kind ('scan' | 'integrity'), [PlaylistRunResult]
    refreshRequested = Signal()       # ask the app shell to re-scan in the background

    MODE_TITLES = {"sync": "Sync Now", "scan": "Scan", "integrity": "Check Integrity", "copy": "Copy to Device"}

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._rows: Dict[str, PlaylistRow] = {}
        # v0.16.50 (Checklist items 41b / 41c): the artists' bars, by artist
        # ID, and the GUI-thread call a scan / sync uses to add a watched
        # artist's new releases.
        self._artist_rows: Dict[str, ArtistRow] = {}
        self.apply_new_releases_call = ui_backend.GuiCall(self._apply_new_releases, self)
        self._expanded_keys = set()
        self._status: Dict[str, dict] = {}
        self._mode: Optional[str] = None
        self._remove_mode = False
        self._control: Optional[ui_backend.SyncControl] = None
        self._running_kind: Optional[str] = None
        self._refreshing = False
        self._last_sync_report = ""
        # "Cancel Scan, Use Cache" (v0.16.14)
        self._scan_control: Optional[ui_backend.SyncControl] = None
        self._discard_scan_updates = False  # a cancelled scan's late signals are ignored
        self._using_cache = False           # Status card says the numbers are cached
        # Keyboard control (v0.16.20): what the highlight is on --
        # ("row", key), ("edit", key) for a row's Edit button, or ("w", widget)
        self._kbd_target: Optional[tuple] = None
        self._space_held = False   # Space held while choosing: W/S tick a run of playlists
        self._space_state = False  # the state those playlists take
        # Search bar (v0.16.30): its text when the typing cursor went in,
        # for Esc to put back; the choosing hint without the hidden-ticks line
        self._search_before = ""
        self._select_hint_base = "Tick the playlists to include."
        # v0.16.32: the search the list was last filtered by (to notice it
        # being cleared), and the playlist the selector was on when Ctrl+F
        # moved it to the search bar.
        self._last_query = ""
        self._search_from: Optional[str] = None
        # v0.16.35 (Checklist item 36): the playlist whose last single click
        # opened / closed it, so a double click doesn't undo that
        self._click_toggled: Optional[str] = None
        # Add queue (v0.16.33, Checklist item 34): the playlists of the Add
        # run going on (None: no Add run), the one being added now, and
        # whether Cancel all was pressed.
        self._adds: Optional[List[QueuedAdd]] = None
        self._add_current: Optional[QueuedAdd] = None
        self._adds_cancel_all = False
        # Sync Now's playlists (v0.16.36, Checklist item 37), for the queue
        # list and its ✕ on the one being synced (None: no Sync Now running)
        self._syncs: Optional[List[QueuedSync]] = None
        self._scheduled_run = False  # v0.25.0: the running Sync Now was started by the scheduled task

        root = QHBoxLayout(self)
        root.setContentsMargins(26, 6, 26, 22)
        root.setSpacing(22)

        # ================= left column =================================
        left = QVBoxLayout()
        left.setSpacing(10)
        root.addLayout(left, 1)

        head = QHBoxLayout()
        head_text = QVBoxLayout()
        head_text.setSpacing(0)
        head_text.addWidget(CapsLabel("Synced playlists"))
        self.folder_title = QLabel("Folder Name")
        self.folder_title.setProperty("role", "title")
        head_text.addWidget(self.folder_title)
        head.addLayout(head_text, 1)
        self.add_btn = make_button("+  Add", large=True)
        self.add_btn.clicked.connect(self._on_add)
        head.addWidget(self.add_btn, 0, Qt.AlignBottom)
        left.addLayout(head)

        # v0.16.29 (Checklist item 30): open / close every playlist's
        # details at once. Like the chevrons, they work while a task runs.
        list_tools = QHBoxLayout()
        list_tools.setSpacing(8)
        # v0.16.30 (Checklist item 31): show only the playlists whose name
        # contains every word typed. Works in every mode, like the buttons.
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search playlists")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Show only the playlists whose name contains every word typed")
        self.search.setMinimumWidth(160)
        self.search.setMaximumWidth(340)
        self.search.textChanged.connect(self._apply_filter)
        self.search.returnPressed.connect(self._search_entered)
        self.search.installEventFilter(self)
        list_tools.addWidget(self.search, 3)
        list_tools.addStretch(1)
        self.expand_all_btn = make_button("Expand all", compact=True)
        self.expand_all_btn.setToolTip("Show every playlist's details")
        self.expand_all_btn.clicked.connect(lambda: self._set_all_expanded(True))
        self.collapse_all_btn = make_button("Collapse all", compact=True)
        self.collapse_all_btn.setToolTip("Hide every playlist's details")
        self.collapse_all_btn.clicked.connect(lambda: self._set_all_expanded(False))
        list_tools.addWidget(self.expand_all_btn)
        list_tools.addWidget(self.collapse_all_btn)
        left.addLayout(list_tools)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 2, 6, 2)
        self.rows_layout.setSpacing(12)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.rows_host)
        smooth_scroll(self.scroll)  # v0.17.0
        left.addWidget(self.scroll, 1)
        # v0.17.0: bars slide to their new places when one is reordered
        self._reorder_anim = make_value_anim(self, self._reorder_step, ANIM_REORDER_MS, motion_curve())
        self._reorder_moves: list = []  # (row, from y, to y)

        self.empty_label = QLabel("No playlists yet — press “+ Add” to sync your first one.")
        self.empty_label.setProperty("role", "muted")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.rows_layout.insertWidget(0, self.empty_label)

        # v0.16.49: albums are their own list, under this heading, below
        # every playlist. Placed by reload(); hidden when no album shows.
        self.albums_header = QWidget()
        ah = QVBoxLayout(self.albums_header)
        ah.setContentsMargins(2, 14, 0, 0)
        ah.setSpacing(0)
        ah.addWidget(CapsLabel("Albums & discographies"))
        self.albums_header.hide()

        bottom_row = QHBoxLayout()
        self.remove_hint = QLabel("")
        self.remove_hint.setProperty("role", "bad")
        bottom_row.addWidget(self.remove_hint, 1)
        self.remove_btn = make_button("Remove", "danger", large=True)
        self.remove_btn.clicked.connect(self._toggle_remove_mode)
        bottom_row.addWidget(self.remove_btn)
        left.addLayout(bottom_row)

        self.panel = SyncPanel()
        self.panel.pauseClicked.connect(self._on_pause_clicked)
        self.panel.cancelClicked.connect(self._on_cancel_clicked)
        self.panel.detailsClicked.connect(self._show_details)
        self.panel.queueItemCancel.connect(self._on_queue_item_cancel)
        left.addWidget(self.panel.drawer)  # v0.17.0: the panel slides in its drawer

        # ================= right column ================================
        right = QVBoxLayout()
        right.setSpacing(16)
        right_host = QWidget()
        right_host.setFixedWidth(384)
        right_host.setLayout(right)
        root.addWidget(right_host, 0)

        # ---- Actions
        actions = make_panel()
        a_lay = QVBoxLayout(actions)
        a_lay.setContentsMargins(20, 18, 20, 20)
        a_lay.setSpacing(12)
        a_lay.addWidget(CapsLabel("Actions", muted=True))
        self.actions_stack = QStackedWidget()
        a_lay.addWidget(self.actions_stack)
        right.addWidget(actions)

        page_buttons = QWidget()
        pb = QVBoxLayout(page_buttons)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.setSpacing(10)
        self.sync_btn = make_button("Sync Now", "primary")
        self.sync_btn.setMinimumHeight(42)
        self.scan_btn = make_button("Scan")
        self.scan_btn.setMinimumHeight(42)
        self.integrity_btn = make_button("Check Integrity")
        self.integrity_btn.setMinimumHeight(42)
        self.sync_btn.clicked.connect(lambda: self._enter_mode("sync"))
        self.scan_btn.clicked.connect(lambda: self._enter_mode("scan"))
        self.integrity_btn.clicked.connect(lambda: self._enter_mode("integrity"))
        # v0.26.0 (Checklist item 66): copy chosen playlists to a USB stick / SD card / phone folder
        self.copy_btn = make_button("Copy to Device")
        self.copy_btn.setMinimumHeight(42)
        self.copy_btn.setToolTip("Copy playlists to a USB stick, SD card or phone folder, keeping them matched")
        self.copy_btn.clicked.connect(lambda: self._enter_mode("copy"))
        for b in (self.sync_btn, self.scan_btn, self.integrity_btn, self.copy_btn):
            pb.addWidget(b)
        self.actions_stack.addWidget(page_buttons)

        page_select = QWidget()
        ps = QVBoxLayout(page_select)
        ps.setContentsMargins(0, 0, 0, 0)
        ps.setSpacing(10)
        self.select_title = QLabel("Choose playlists")
        stf = self.select_title.font()
        stf.setPointSizeF(13)
        stf.setBold(True)
        self.select_title.setFont(stf)
        ps.addWidget(self.select_title)
        self.select_hint = QLabel("Tick the playlists to include.")
        self.select_hint.setProperty("role", "muted")
        self.select_hint.setWordWrap(True)
        ps.addWidget(self.select_hint)
        self.toggle_all_btn = make_button("Uncheck all")
        self.toggle_all_btn.clicked.connect(self._toggle_all)
        ps.addWidget(self.toggle_all_btn)
        confirm_row = QHBoxLayout()
        confirm_row.setSpacing(10)
        self.mode_cancel_btn = make_button("Cancel")
        self.mode_cancel_btn.clicked.connect(self._exit_mode)
        self.mode_go_btn = make_button("Start", "primary")
        self.mode_go_btn.clicked.connect(self._run_mode)
        confirm_row.addWidget(self.mode_cancel_btn)
        confirm_row.addWidget(self.mode_go_btn, 1)
        ps.addLayout(confirm_row)
        self.actions_stack.addWidget(page_select)

        # ---- Music Folder
        folder = make_panel()
        f_lay = QVBoxLayout(folder)
        f_lay.setContentsMargins(20, 18, 20, 18)
        f_lay.setSpacing(10)
        f_lay.addWidget(CapsLabel("Music folder", muted=True))
        f_row = QHBoxLayout()
        f_row.setSpacing(10)
        box = make_panel("inputbox")
        box_lay = QHBoxLayout(box)
        box_lay.setContentsMargins(12, 8, 12, 8)
        self.path_label = ElidedLabel("", mode=Qt.ElideMiddle)
        box_lay.addWidget(self.path_label)
        f_row.addWidget(box, 1)
        self.change_btn = make_button("Change")
        self.change_btn.clicked.connect(self._on_change_folder)
        f_row.addWidget(self.change_btn)
        f_lay.addLayout(f_row)
        right.addWidget(folder)

        # ---- Status
        status = make_panel()
        s_lay = QVBoxLayout(status)
        s_lay.setContentsMargins(20, 18, 20, 18)
        s_lay.setSpacing(6)
        s_lay.addWidget(CapsLabel("Status", muted=True))
        st_row = QHBoxLayout()
        st_row.setSpacing(10)
        self.status_spinner = Spinner(20)
        st_row.addWidget(self.status_spinner)
        self.status_text = QLabel("Checking…")
        sf = self.status_text.font()
        sf.setPointSizeF(15)
        sf.setBold(True)
        self.status_text.setFont(sf)
        st_row.addWidget(self.status_text, 1)
        s_lay.addLayout(st_row)
        # v0.16.27 (Checklist item 28): "N no match · M unavailable" in red,
        # on its own line under the pending count.
        self.status_red = QLabel("")
        rf = self.status_red.font()
        rf.setBold(True)
        self.status_red.setFont(rf)
        self.status_red.setProperty("role", "bad")
        self.status_red.setWordWrap(True)
        self.status_red.hide()
        s_lay.addWidget(self.status_red)
        self.status_sub = QLabel("")
        self.status_sub.setProperty("role", "muted")
        self.status_sub.setWordWrap(True)
        s_lay.addWidget(self.status_sub)
        # Shown only while the launch-time (background) scan runs; the
        # Scan button's own scan has the same button in the progress panel.
        self.use_cache_btn = make_button("Cancel Scan, Use Cache", compact=True)
        self.use_cache_btn.setToolTip("Stop checking the live playlists and keep the last known numbers")
        self.use_cache_btn.clicked.connect(self._cancel_refresh_use_cache)
        self.use_cache_btn.hide()
        s_lay.addSpacing(4)
        s_lay.addWidget(self.use_cache_btn, 0, Qt.AlignLeft)
        right.addWidget(status)
        right.addStretch(1)

        # v0.27.0: the Support button (a lit heart), bottom right; opens
        # Michael's note and Ko-fi link. Placed by hand in the page's
        # bottom-right corner (_place_support_btn), outside the layout, so
        # it never takes room from the Actions panel in the smallest window.
        self._right_host = right_host
        self._status_card = status
        status.installEventFilter(self)
        self.support_btn = HeartButton(50, self)
        self.support_btn.setToolTip("Support Mud Music Manager")
        self.support_btn.clicked.connect(self._on_support)

        self._clock = QTimer(self)
        self._clock.setInterval(20000)
        self._clock.timeout.connect(self.refresh_status_card)
        self._clock.start()

        self.reload()

    # ------------------------------------------------------------------
    # Building the list
    # ------------------------------------------------------------------
    @property
    def config(self) -> dict:
        return self.ctx.config

    @property
    def backend(self) -> ui_backend.Backend:
        return self.ctx.backend

    def reload(self) -> None:
        """Rebuild everything from ctx.config (after add/remove/rename/
        reorder, or the playlists file changing on disk)."""
        self._stop_reorder_slide()  # v0.17.0: its bars are about to go
        lib = config_manager.get_library_path(self.config)
        self.folder_title.setText((os.path.basename(lib.rstrip("/\\")) or lib) if lib else "Music folder not set")
        self.path_label.setText(lib or "(not set)")
        self.path_label.setToolTip(lib or "")

        # Keep the ticks if the list is rebuilt while choosing playlists
        # (e.g. playlists.json changed on disk); new playlists start unticked.
        ticked = {k for k, r in self._rows.items() if r.is_checked()} if self._mode else set()
        # A position box about to be deleted mustn't keep the focus (v0.16.23).
        for row in self._rows.values():
            _park_focus(row, self)
        for row in list(self._rows.values()):
            self.rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        for arow in list(self._artist_rows.values()):
            _park_focus(arow, self)
            self.rows_layout.removeWidget(arow)
            arow.deleteLater()
        self._artist_rows.clear()
        self.rows_layout.removeWidget(self.albums_header)
        self.albums_header.hide()

        # v0.16.49: playlists first, then the albums under their own
        # heading. config_manager keeps the list in that order, and each
        # row can only be moved within its own section.
        # v0.16.50 (Checklist items 41b / 41c): in the albums, each tracked
        # artist's bar comes first with its albums under it (indented), in
        # the order the artists were added, then the albums added on their own.
        config_manager.group_playlists(self.config)
        playlists = config_manager.list_playlists(self.config)
        total = len(playlists)
        artists = config_manager.list_artists(self.config)
        sections = {}
        for i, entry in enumerate(playlists, start=1):
            sections.setdefault(config_manager.section_key(self.config, entry), []).append((i, entry))
        order = [("playlist",)]
        for artist in artists:
            order.append(("artist", config_manager.artist_id_of(artist)))
        order.append(("album",))
        items = []  # (position, entry) or (None, artist)
        for key in order:
            if key[0] == "artist":
                artist = config_manager.find_artist(self.config, key[1])
                items.append((None, artist))
            items.extend(sections.get(key, []))
        for pos, item in items:
            if (pos is None or config_manager.in_albums_section(item)) and \
                    self.rows_layout.indexOf(self.albums_header) < 0:
                self.rows_layout.insertWidget(self.rows_layout.count() - 1, self.albums_header)
            if pos is None:
                arow = ArtistRow(item, len(sections.get(("artist", config_manager.artist_id_of(item)), [])))
                arow.watchToggled.connect(self._on_artist_watch)
                arow.addRequested.connect(self._on_artist_add_more)
                arow.removePicked.connect(self._on_artist_remove)
                arow.checkToggled.connect(self._on_artist_ticked)
                arow.expandedChanged.connect(self._on_expanded_changed)
                arow.revealWanted.connect(self._reveal_row)
                self.rows_layout.insertWidget(self.rows_layout.count() - 1, arow)
                self._artist_rows[arow.artist_id] = arow
                if arow.key in self._expanded_keys:
                    arow.set_expanded(True, animate=False)
                if self._mode:
                    arow.set_select_mode(True)
                if self._remove_mode:
                    arow.set_pick_mode(True)
                continue
            i, entry = pos, item
            row = PlaylistRow(entry, i, total)
            row.set_position(i, total, *config_manager.section_range(self.config, entry))
            if config_manager.section_key(self.config, entry)[0] == "artist":
                row.set_indent(32)
            row.editRequested.connect(self._on_edit)
            row.moveRequested.connect(self._on_move)
            row.positionChosen.connect(self._on_position_chosen)
            row.removePicked.connect(self._on_remove_picked)
            row.expandedChanged.connect(self._on_expanded_changed)
            row.revealWanted.connect(self._reveal_row)
            row.checkedChanged.connect(self._update_select_ui)
            row.rowClicked.connect(self._on_row_clicked)
            row.expandClicked.connect(self._on_row_expand_click)
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
            self._rows[row.key] = row
            if row.key in self._expanded_keys:
                row.set_expanded(True, animate=False)
            self._apply_cached_status(row)
            if self._mode:
                row.set_select_mode(True, row.key in ticked)
            if self._remove_mode:
                row.set_pick_mode(True)
        # A playlist whose link changed (Edit page or CMD menu) has a new
        # key; drop states for keys no longer listed so the Status card
        # doesn't keep counting the old one.
        for key in [k for k in self._status if k not in self._rows]:
            del self._status[key]
        self._sync_artist_ticks()
        self._apply_filter()  # also the empty-list text and Check all / Start
        self.refresh_status_card()
        self.set_busy(self.backend.heavy_running)
        self._kbd_follow_rebuild()

    @staticmethod
    def _counts_state(counts: dict, **extra) -> dict:
        """A row state from pending / no_match / unavailable counts (a
        cache entry, or ui_backend.scan_attention / sync_attention)."""
        pending = int(counts.get("pending") or 0)
        no_match = int(counts.get("no_match") or 0)
        unavailable = int(counts.get("unavailable") or 0)
        state = {"state": "ok" if not (pending or no_match or unavailable) else "pending",
                 "pending": pending, "no_match": no_match, "unavailable": unavailable}
        state.update(extra)
        return state

    def _apply_cached_status(self, row: PlaylistRow) -> None:
        state = self._status.get(row.key)
        if state is None:
            cache = ui_settings.get_cache(row.key)
            if cache.get("scanned_at") is not None and cache.get("pending") is not None:
                state = self._counts_state(cache, cached=True)
            else:
                state = {"state": "unknown"}
            self._status[row.key] = state
        self._render_status(row, state)

    def _render_status(self, row: PlaylistRow, state: dict) -> None:
        kind = state.get("state")
        tip = ""
        if state.get("error"):
            tip = f"Couldn't refresh: {state['error']}"
        elif state.get("cached"):
            cache = ui_settings.get_cache(row.key)
            tip = f"Last checked {relative_time(cache.get('scanned_at'))}"
        if kind == "ok":
            row.set_status("In Sync", "ok", tip)
        elif kind == "pending":
            pending = state.get("pending", 0)
            no_match, unavailable = state.get("no_match", 0), state.get("unavailable", 0)
            red = ui_backend.red_text(no_match, unavailable)
            red_tip = ui_backend.red_tooltip(no_match, unavailable)
            if tip:
                red_tip = f"{red_tip}\n{tip}"
            row.set_status(f"{pending} pending" if (pending or not red) else "", "warn", tip, red, red_tip)
        elif kind == "checking":
            row.set_status("Checking…", "info")
        elif kind == "syncing":
            row.set_status("Syncing…", "info")
        else:
            row.set_status("Not checked", "neutral", tip)

    # ------------------------------------------------------------------
    # Updates pushed in from background threads
    # ------------------------------------------------------------------
    def on_local_stats(self, key: str, stats: dict) -> None:
        row = self._rows.get(key)
        if row:
            row.refresh_text(stats)

    def on_entry_started(self, entry: dict, syncing: bool = False) -> None:
        if self._discard_scan_updates:  # a cancelled scan winding down
            return
        key = ui_settings.playlist_key(entry)
        row = self._rows.get(key)
        if row is None:  # removed, or its link changed, while the task ran
            return
        self._using_cache = False
        self._status[key] = {"state": "syncing" if syncing else "checking"}
        self._render_status(row, self._status[key])
        self.refresh_status_card()

    def on_entry_done(self, run: "ui_backend.PlaylistRunResult") -> None:
        if self._discard_scan_updates:  # a cancelled scan winding down
            return
        key = ui_settings.playlist_key(run.entry)
        if key not in self._rows:  # removed, or its link changed, while the task ran
            return
        if run.stopped:  # v0.16.36: its sync was stopped; back to its cached status
            self._status.pop(key, None)
            row = self._rows[key]
            row.refresh_text(ui_settings.get_cache(key) or None)
            self._apply_cached_status(row)
            self.refresh_status_card()
            return
        prev_cache = ui_settings.get_cache(key)
        if run.error:
            cached_pending = prev_cache.get("pending")
            if cached_pending is not None and prev_cache.get("scanned_at") is not None:
                st = self._counts_state(prev_cache, cached=True, error=run.error)
            else:
                st = {"state": "unknown", "error": run.error}
        elif run.kind == "sync":
            st = self._counts_state(ui_backend.sync_attention(run.result))
        elif run.kind == "scan":
            st = self._counts_state(ui_backend.scan_attention(run.result))
        else:
            st = self._status.get(key) or {"state": "unknown"}
        self._status[key] = st
        row = self._rows.get(key)
        if row:
            row.refresh_text(ui_settings.get_cache(key) or None)
            self._render_status(row, st)
        self.refresh_status_card()

    def set_refreshing(self, on: bool) -> None:
        self._refreshing = on
        if on:
            self._using_cache = False
        self.use_cache_btn.setVisible(on)
        self.refresh_status_card()

    def on_refresh_finished(self) -> None:
        """The launch-time scan ended: finished, cancelled with "Cancel
        Scan, Use Cache", stopped for a Sync/Scan/Save, or failed. A row
        still at "Checking…" had its read stopped mid-way, so it goes back
        to its cached status."""
        self._discard_scan_updates = False
        self._refreshing = False
        self.use_cache_btn.hide()
        self._show_cached_for_checking()

    # ------------------------------------------------------------------
    # "Cancel Scan, Use Cache" (v0.16.14)
    # ------------------------------------------------------------------
    def _show_cached_for_checking(self) -> None:
        """Every row still at "Checking…" shows its cached status again
        (In Sync / N pending from the last check, or Not checked)."""
        for key in [k for k, s in self._status.items() if s.get("state") == "checking"]:
            del self._status[key]
        for row in self._rows.values():
            self._apply_cached_status(row)
        self.refresh_status_card()

    def _use_cached_state(self) -> None:
        self._discard_scan_updates = True
        self._using_cache = True
        self._show_cached_for_checking()

    def show_launch_scan_skipped(self) -> None:
        """Settings → Scan on launch is Off (v0.16.17): the rows already
        show their cached status; the Status card says the numbers are
        cached, as after "Cancel Scan, Use Cache"."""
        self._using_cache = True
        self.refresh_status_card()

    def _cancel_refresh_use_cache(self) -> None:
        """Status card button: stop the launch-time scan. The screen shows
        the cached numbers at once; the background read stops at its next
        safe point (at worst when the current playlist's live read returns)
        and its result is thrown away."""
        _park_focus(self.use_cache_btn, self)
        self.use_cache_btn.hide()
        if not self.backend.cancel_refresh():
            return
        self._refreshing = False
        self._use_cached_state()

    def cancel_launch_scan(self) -> bool:
        """X on the Edit page (v0.16.23): same as the Status card's "Cancel
        Scan, Use Cache" button, which isn't on screen there. False if the
        launch-time scan isn't running."""
        if not self._refreshing:
            return False
        self._cancel_refresh_use_cache()
        return True

    def _cancel_scan_use_cache(self) -> None:
        """Progress panel button during a Scan: same as above, and no
        results page is shown."""
        control = self._scan_control
        if control is None or control.cancel_requested:
            return
        control.request_cancel()
        self._use_cached_state()
        _park_focus(self.panel, self)
        self.panel.cancel_btn.setEnabled(False)
        self.panel.set_text("Scan cancelled — showing cached data. Stopping the current playlist read…")

    def _finish_scan_cancel(self) -> None:
        self._scan_control = None
        self._discard_scan_updates = False
        self._show_cached_for_checking()
        self.set_busy(self.backend.heavy_running)
        self.panel.hide_panel()

    def refresh_status_card(self) -> None:
        states = [s.get("state") for s in self._status.values()]
        counted = [s for s in self._status.values() if s.get("state") == "pending"]
        pending_total = sum(s.get("pending", 0) for s in counted)
        pending_lists = sum(1 for s in counted if s.get("pending", 0))
        no_match_total = sum(s.get("no_match", 0) for s in counted)
        unavailable_total = sum(s.get("unavailable", 0) for s in counted)
        busy = self._refreshing or any(s in ("checking", "syncing") for s in states)
        red_parts = []
        if no_match_total:
            red_parts.append(f"{no_match_total} no match")
        if unavailable_total:
            red_parts.append(f"{unavailable_total} unavailable")
        red_line = ""
        if busy:
            self.status_spinner.start()
            text, role = ("Syncing…" if "syncing" in states else "Checking…"), ""
        else:
            self.status_spinner.stop()
            if not states:
                text, role = "No playlists yet", "muted"
            elif counted and pending_total:
                text = f"{pending_total} pending in {pending_lists} playlist{'s' if pending_lists != 1 else ''}"
                role = "warn"
                red_line = " · ".join(red_parts)
            elif counted:
                text, role = " · ".join(red_parts), "bad"
            elif any(s == "unknown" for s in states):
                text, role = "Not fully checked", "muted"
            else:
                text, role = "All up to date", "ok"
        self.status_red.setText(red_line)
        self.status_red.setVisible(bool(red_line))
        self.status_text.setText(text)
        self.status_text.setProperty("role", role)
        _repolish(self.status_text)
        last = ui_settings.get("last_scan_at")
        if self._using_cache and not busy:
            self.status_sub.setText(f"Using cached data · last check: {relative_time(last)}")
        else:
            self.status_sub.setText(f"Last check: {relative_time(last)}")

    # ------------------------------------------------------------------
    # Row interactions
    # ------------------------------------------------------------------
    def _on_expanded_changed(self, key: str, expanded: bool) -> None:
        if expanded:
            self._expanded_keys.add(key)
        else:
            self._expanded_keys.discard(key)

    def _set_all_expanded(self, expanded: bool) -> None:
        """Expand all / Collapse all (v0.16.29, Checklist item 30): the same
        as clicking every chevron that would change, so the open set is
        remembered across list reloads as usual. Collapsing hides the Edit
        buttons, so an outline on one moves to its playlist first. While
        searching (v0.16.30) only the playlists shown change."""
        target = self._kbd_target
        if not expanded and target is not None and target[0] == "edit":
            ring = self._key_ring()
            if ring is not None and ring.is_shown(self):
                self._kbd_show(("row", target[1]), scroll=False)
            else:
                self._kbd_target = ("row", target[1])
        for row in self._shown_rows():
            row.set_expanded(expanded)

    # ------------------------------------------------------------------
    # Search bar (v0.16.30, Checklist item 31)
    # ------------------------------------------------------------------
    def _name_matches(self, name: str) -> bool:
        """Every word typed appears somewhere in the name, in any order,
        ignoring case ("punk daft" finds "Daft Punk")."""
        name = name.lower()
        return all(word in name for word in self.search.text().lower().split())

    def _shown_rows(self) -> List[PlaylistRow]:
        return [r for r in self._rows.values() if not r.isHidden()]

    def _apply_filter(self) -> None:
        """Show only the matching playlists. Called as the search text
        changes and after every list rebuild. Hiding a row changes nothing
        else about it: its tick, its open details and its position stay."""
        for row in self._rows.values():
            show = self._name_matches(row.entry["folder_name"])
            if not show:
                _park_focus(row, self)  # a position box being typed in
            row.setVisible(show)
        # v0.16.50: an artist's bar shows while its name matches or one of
        # its albums is shown.
        for artist_id, arow in self._artist_rows.items():
            arow.setVisible(self._name_matches(arow.artist.get("name") or "") or any(
                not r.isHidden() and config_manager.entry_artist_id(self.config, r.entry) == artist_id
                for r in self._rows.values()))
        shown = self._shown_rows()
        # v0.16.49: the Albums heading only while an album is shown
        self.albums_header.setVisible(
            (any(config_manager.in_albums_section(r.entry) for r in shown)
             or any(not a.isHidden() for a in self._artist_rows.values()))
            and self.rows_layout.indexOf(self.albums_header) >= 0)
        if not self._rows:
            self.empty_label.setText("No playlists yet — press “+ Add” to sync your first one.")
        else:
            self.empty_label.setText(f"No playlists match “{self.search.text().strip()}”.")
        self.empty_label.setVisible(not shown)
        for b in (self.expand_all_btn, self.collapse_all_btn):
            if not shown:
                _park_focus(b, self)
            b.setEnabled(bool(shown))
        self._update_select_ui()
        # an outline on a playlist that is now hidden moves to the first one shown
        ring = self._key_ring()
        if ring is not None and ring.is_shown(self) and self._kbd_resolve(self._kbd_target) is None:
            self._kbd_show(self._kbd_default())
        # v0.16.32: clearing the search scrolls the full list to the
        # selected playlist (once the rows just shown are laid out).
        query = self.search.text().strip()
        if self._last_query and not query:
            key = self._selected_playlist()
            if key is not None:
                QTimer.singleShot(0, lambda k=key: self._centre_on(k))
        self._last_query = query

    def _selected_playlist(self) -> Optional[str]:
        """The playlist the keyboard selector is on (or on its Edit
        button); while typing in the search bar, the one it was on before
        Ctrl+F (v0.16.32)."""
        target = self._kbd_target
        if target is not None and target[0] in ("row", "edit"):
            key = target[1]
        elif target is not None and target[1] is self.search:
            key = self._search_from
        else:
            key = None
        return key if key in self._rows else None

    def _centre_on(self, key: str) -> None:
        """Scroll so this playlist sits in the middle of the list."""
        row = self._rows.get(key)
        if row is not None and ui_keys.is_valid(row) and not row.isHidden():
            scroll_area_to(self.scroll, row.y() + row.height() // 2 - self.scroll.viewport().height() // 2)

    def _search_entered(self) -> None:
        """Enter in the search bar keeps the search and leaves the bar (the
        keyboard controller does that); the outline goes to the first
        playlist shown, so W/S, E, Space... carry on from there."""
        shown = self._shown_rows()
        if shown:
            self._kbd_show(("row", shown[0].key))

    def _kbd_search(self) -> None:
        """Ctrl+F or /: put the typing cursor in the search bar with its
        text selected, so typing replaces it. The outline hides while
        typing, as it does for a dialog's text field."""
        if not ui_keys.usable(self.search):
            return
        if self._kbd_target is not None and self._kbd_target[0] in ("row", "edit"):
            self._search_from = self._kbd_target[1]  # scrolled back to when the search is cleared
        self._kbd_target = ("w", self.search)
        ring = self._key_ring()
        if ring is not None and ring.is_shown(self):
            ring.hide_ring()
        self.search.setFocus(Qt.ShortcutFocusReason)
        self.search.selectAll()

    def eventFilter(self, obj, e):
        if obj is self.search and e.type() == QEvent.FocusIn:
            self._search_before = self.search.text()  # for Esc to put back
        elif obj is getattr(self, "_status_card", None) and e.type() in (QEvent.Resize, QEvent.Move):
            self._place_support_btn()
        return super().eventFilter(obj, e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_support_btn()

    def _place_support_btn(self) -> None:
        """v0.27.0: the heart sits in the bottom-right corner, lined up with
        the right column's edge and the page's bottom margin. Hidden in the
        rare case the Status card has grown down into its spot (a very
        small window with the card's extra lines showing)."""
        btn = self.support_btn
        host = self._right_host.geometry()
        margins = self.layout().contentsMargins()
        x = host.right() + 1 - btn.width()
        y = self.height() - margins.bottom() - btn.height()
        btn.move(x, y)
        card_bottom = self._status_card.mapTo(self, self._status_card.rect().bottomLeft()).y()
        btn.setVisible(card_bottom + 8 <= y)
        btn.raise_()

    def _on_edit(self, folder_name: str) -> None:
        entry = config_manager.find_playlist(self.config, folder_name)
        if entry:
            self.openEdit.emit(entry)

    def _on_move(self, folder_name: str, delta: int) -> None:
        playlists = config_manager.list_playlists(self.config)
        idx = next((i for i, e in enumerate(playlists, 1) if e["folder_name"] == folder_name), None)
        if idx is None:
            return
        self._reorder(folder_name, idx + delta)

    def _on_position_chosen(self, folder_name: str, position: int) -> None:
        self._reorder(folder_name, position)

    def _reorder(self, folder_name: str, position: int) -> None:
        # v0.17.0: where every bar is on screen now (mid-slide if one is
        # still moving), so the rebuilt bars can slide from there
        before = {k: r.y() for k, r in self._rows.items() if r.isVisible()}
        try:
            config_manager.reorder_playlist(self.config, folder_name, position)
        except ValueError:
            self.reload()
            return
        # v0.17.0: this save is ours. Without this, the window coming back
        # to the front took it for a change made elsewhere (the CMD window)
        # and reloaded the list, cutting the slide off.
        self.ctx.remember_mtimes()
        entry = config_manager.find_playlist(self.config, folder_name)
        # v0.17.0: the bars are the same, only their order changed, so they
        # are moved in place (~2 ms) instead of all being rebuilt (~250 ms
        # for 19 bars: the delay before each move). Anything unexpected
        # falls back to the full rebuild.
        if entry is None or not self._reorder_rows_in_place(entry):
            self.reload()
        self._slide_rows(before, ui_settings.playlist_key(entry) if entry else None)

    def _reorder_rows_in_place(self, entry: dict) -> bool:
        """Put the existing bars of `entry`'s section in the new saved order
        and renumber every bar. False (nothing changed) if the bars don't
        match the saved list one to one."""
        playlists = config_manager.list_playlists(self.config)
        by_id = {id(r.entry): r for r in self._rows.values()}
        if len(playlists) != len(self._rows) or any(id(e) not in by_id for e in playlists):
            return False
        section = config_manager.section_key(self.config, entry)
        rows = [by_id[id(e)] for e in playlists if config_manager.section_key(self.config, e) == section]
        slots = sorted(self.rows_layout.indexOf(r) for r in rows)
        if not slots or slots[0] < 0:
            return False
        self._stop_reorder_slide()
        for r in rows:
            _park_focus(r, self)  # as the rebuild did: no typing cursor left in a position box
            self.rows_layout.removeWidget(r)
        for slot, r in zip(slots, rows):  # ascending, so every slot lands where it was
            self.rows_layout.insertWidget(slot, r)
        total = len(playlists)
        for i, e in enumerate(playlists, start=1):
            by_id[id(e)].set_position(i, total, *config_manager.section_range(self.config, e))
        self.set_busy(self.backend.heavy_running)  # set_position re-enables ▲ / ▼
        self._kbd_follow_rebuild()  # the outline stays on its bar, scrolled into view
        return True

    # ---- sliding reorder (v0.17.0) ---------------------------------------
    def _slide_rows(self, before: Dict[str, int], moved_key: Optional[str]) -> None:
        """After a reorder rebuilt the list: every bar whose place changed
        starts where it was and slides to its new place, the moved bar on
        top of the ones it passes. Purely visual; the order is already
        saved, and the next layout pass puts every bar where it belongs."""
        self._stop_reorder_slide()
        if not animations_on() or not before or not self.isVisible():
            return
        self.rows_layout.activate()
        moves = []
        for key, row in self._rows.items():
            y0 = before.get(key)
            if y0 is not None and row.isVisible() and y0 != row.y():
                moves.append((row, y0, row.y()))
                row.move(row.x(), y0)
        if not moves:
            return
        moved = self._rows.get(moved_key) if moved_key else None
        if moved is not None:
            moved.raise_()
        self._reorder_moves = moves
        run_value_anim(self._reorder_anim, 0.0, 1.0)

    def _reorder_step(self, t: float) -> None:
        try:
            for row, y0, y1 in self._reorder_moves:
                row.move(row.x(), round(y0 + (y1 - y0) * t))
        except RuntimeError:  # the list was rebuilt meanwhile
            self._reorder_anim.stop()
            self._reorder_moves = []

    def _stop_reorder_slide(self) -> None:
        self._reorder_anim.stop()
        self._reorder_moves = []

    # ---- scroll an opened bar into view (v0.17.0) -------------------------
    def _reveal_row(self, row) -> None:
        """A bar the user just opened: if its details would end below the
        list's edge, glide down so the whole bar shows (never past its
        top), while it opens."""
        if not ui_keys.is_valid(row) or not row.isVisible():
            return
        smooth = getattr(self.scroll, "_mmm_smooth", None)
        top = row.y()
        bottom = top + row.height() - row.drawer.height() + row.drawer.full_height()
        vh = self.scroll.viewport().height()
        cur = smooth.heading() if smooth is not None else self.scroll.verticalScrollBar().value()
        margin = 10
        if bottom + margin <= cur + vh:
            return
        target = min(top - margin, bottom + margin - vh)
        if smooth is not None and smooth_scrolling_on():
            smooth.scroll_to(target, clamp=False)  # the list grows as the bar opens
        else:
            # v0.17.4: smooth scrolling off -- one jump once the bar has opened
            delay = ANIM_OPEN_MS + 20 if animations_on() else 0
            QTimer.singleShot(delay, lambda: ui_keys.is_valid(row) and self.scroll.ensureWidgetVisible(row, 0, margin))

    # ------------------------------------------------------------------
    # Add / Remove / Change folder
    # ------------------------------------------------------------------
    def _on_add(self) -> None:
        """+ Add, or, while a playlist is being added, "Queue Another
        Playlist" (v0.16.33, Checklist item 34): the same dialog, and the
        playlist waits its turn instead of being added straight away."""
        prefill_link = prefill_name = ""
        while True:
            dlg = ui_dialogs.AddPlaylistDialog(self, prefill_link, prefill_name,
                                               queueing=self._adds is not None)
            if dlg.exec() != dlg.Accepted or not dlg.result_data:
                return
            data = dlg.result_data
            if data.get("mode") == "artist":  # v0.16.50 (41b): choose its releases
                self._add_artist(data["artist_id"])
                return
            # The Add run may have ended while the dialog was open; then this
            # one is added at once, as a normal Add.
            if self._adds is not None:
                problem = self._queue_problem(data)
                if problem is None:
                    self._adds.append(QueuedAdd(data))
                    self._refresh_queue_ui()
                    return
                ui_dialogs.warn(self, "Couldn't queue that playlist", problem)
                prefill_link, prefill_name = data["source"], data["name"]
                continue
            try:
                entry = config_manager.add_playlist(
                    self.config, data["source"], data["name"], custom_path=data["custom_path"],
                    kind=data.get("kind"),  # v0.16.52: a SoundCloud album
                )
            except ValueError as e:
                ui_dialogs.warn(self, "Couldn't add that playlist", str(e))
                prefill_link, prefill_name = data["source"], data["name"]
                continue
            break

        if not self._name_matches(entry["folder_name"]):
            self.search.clear()  # so the new playlist isn't hidden by the search
        self.reload()
        if data["mode"] == "download" and not config_manager.get_library_path(self.config):
            if ui_dialogs.ask_yes_no(
                self, "Choose a Music folder",
                f"“{entry['folder_name']}” was added, but no Music folder is set yet, so there's "
                "nowhere to download to. Choose one now?", "Choose folder", "Later",
            ):
                self._on_change_folder(then_add=(entry, data))
            return
        self._start_add_run(entry, data)

    def _toggle_remove_mode(self) -> None:
        if self.backend.heavy_running:
            return
        self._remove_mode = not self._remove_mode
        if self._remove_mode and self._mode:
            self._exit_mode()
        for row in list(self._rows.values()) + list(self._artist_rows.values()):
            row.set_pick_mode(self._remove_mode)
        self.remove_btn.setText("Cancel" if self._remove_mode else "Remove")
        self.remove_hint.setText("Click the playlist you want to remove." if self._remove_mode else "")

    def _on_remove_picked(self, folder_name: str) -> None:
        dlg = ui_dialogs.ConfirmRemoveDialog(self, folder_name)
        if dlg.exec() != dlg.Accepted:
            return
        entry = config_manager.find_playlist(self.config, folder_name)
        key = ui_settings.playlist_key(entry) if entry else None
        try:
            config_manager.remove_playlist(self.config, folder_name)
        except ValueError as e:
            ui_dialogs.warn(self, "Couldn't remove that playlist", str(e))
            return
        if key:
            ui_settings.forget_playlist(key)
            self._status.pop(key, None)
            self._expanded_keys.discard(key)
        self._remove_mode = False
        self.remove_btn.setText("Remove")
        self.remove_hint.setText("")
        self.reload()

    def _on_support(self) -> None:
        """The heart button (v0.27.0)."""
        ui_dialogs.SupportDialog(self).exec()

    def _on_change_folder(self, then_add: Optional[tuple] = None) -> None:
        """then_add: (entry, dialog data) of a playlist just added with no
        Music folder set; it is synced (as an Add run) once one is chosen."""
        current = config_manager.get_library_path(self.config)
        move = False
        if current:
            dlg = ui_dialogs.ChangeFolderDialog(self, current)
            if dlg.exec() != dlg.Accepted or not dlg.mode:
                return
            move = dlg.mode == "move"
        new_path = QFileDialog.getExistingDirectory(self, "Select the Music Library folder", current or "")
        if not new_path:
            return
        self.ctx.change_library(new_path, move, on_done=lambda ok: self._after_change_folder(ok, move, then_add))

    def _after_change_folder(self, ok: bool, moved: bool, then_add: Optional[tuple]) -> None:
        self.reload()
        if not ok:
            return
        if then_add:
            self._start_add_run(*then_add)
        else:
            # Same as the terminal menu: right after a change, check that
            # every affected playlist is still genuinely in sync there.
            affected = [e for e in config_manager.list_playlists(self.config) if not e.get("custom_path")]
            if affected:
                self._start_scan(affected)

    # ------------------------------------------------------------------
    # Selection mode (Sync Now / Scan / Check Integrity)
    # ------------------------------------------------------------------
    def _enter_mode(self, mode: str) -> None:
        if self.backend.heavy_running:
            return
        playlists = config_manager.list_playlists(self.config)
        if not playlists:
            ui_dialogs.info(self, self.MODE_TITLES[mode], "There are no playlists to work on yet — add one first.")
            return
        if self._remove_mode:
            self._toggle_remove_mode()
        self._mode = mode
        titles = {"sync": ("Sync Now", "Tick the playlists to sync.", "Start Sync"),
                  "scan": ("Scan", "Tick the playlists to scan. A scan only reports what a sync would do.", "Start Scan"),
                  "integrity": ("Check Integrity", "Tick the playlists whose files should be checked.", "Check Files"),
                  "copy": ("Copy to Device", "Tick the playlists to copy. Then choose the device's folder: each "
                           "playlist there is made to match (new songs copied, removed songs deleted there).",
                           "Choose Device…")}
        t, hint, go = titles[mode]
        self.select_title.setText(t)
        self._select_hint_base = hint
        self.mode_go_btn.setText(go)
        for row in list(self._rows.values()) + list(self._artist_rows.values()):
            row.set_select_mode(True)  # every playlist starts unticked
        self.actions_stack.setCurrentIndex(1)
        self._update_select_ui()

    def _exit_mode(self) -> None:
        _park_focus(self.actions_stack, self)  # Start / Cancel are hidden next
        self._mode = None
        for row in list(self._rows.values()) + list(self._artist_rows.values()):
            row.set_select_mode(False)
        self.actions_stack.setCurrentIndex(0)

    def _checked_entries(self) -> List[dict]:
        return [r.entry for r in self._rows.values() if r.is_checked()]

    def _toggle_all(self) -> None:
        """Check all / Uncheck all: while searching (v0.16.30), only the
        playlists shown."""
        rows = self._shown_rows()
        all_checked = all(r.is_checked() for r in rows)
        for r in rows:
            r.check.setChecked(not all_checked)

    # ---- artists (v0.16.50, Checklist items 41b / 41c) -----------------
    def _sync_artist_ticks(self) -> None:
        """An artist's tick shows whether all its albums are ticked."""
        for artist_id, arow in self._artist_rows.items():
            albums = [r for r in self._rows.values()
                      if config_manager.entry_artist_id(self.config, r.entry) == artist_id]
            arow.check.setChecked(bool(albums) and all(r.is_checked() for r in albums))

    def _on_artist_ticked(self, artist_id: str, ticked: bool) -> None:
        for r in self._rows.values():
            if config_manager.entry_artist_id(self.config, r.entry) == artist_id:
                r.check.setChecked(ticked)
        self._update_select_ui()

    def _on_artist_watch(self, artist_id: str, watch: bool) -> None:
        try:
            artist_watch.set_watch(self.config, artist_id, watch)
        except (ValueError, OSError) as e:
            ui_dialogs.warn(self, "Couldn't change watching", str(e))
        self.reload()

    def _on_artist_add_more(self, artist_id: str) -> None:
        if self.backend.heavy_running and self._adds is None:
            ui_dialogs.info(self, "Please wait", "Another task is still running. Try again in a moment.")
            return
        self._add_artist(artist_id)

    def _on_artist_remove(self, artist_id: str) -> None:
        artist = config_manager.find_artist(self.config, artist_id)
        if artist is None:
            return
        albums = artist_watch.artist_albums(self.config, artist_id)
        dlg = ui_dialogs.ConfirmRemoveArtistDialog(self, artist.get("name") or "?", len(albums))
        if dlg.exec() != dlg.Accepted:
            return
        try:
            removed = artist_watch.remove_artist(self.config, artist_id, dlg.albums_box.isChecked())
        except (ValueError, OSError) as e:
            ui_dialogs.warn(self, "Couldn't remove that artist", str(e))
            return
        for entry in removed:
            key = ui_settings.playlist_key(entry)
            ui_settings.forget_playlist(key)
            self._status.pop(key, None)
            self._expanded_keys.discard(key)
        self._remove_mode = False
        self.remove_btn.setText("Remove")
        self.remove_hint.setText("")
        self.reload()

    def _add_artist(self, artist_id: str) -> None:
        """+ Add with an artist link, or an artist's Add releases… (41b):
        the discography picker, then the ticked releases are added and
        synced one after another as an Add run (or join the one running)."""
        if not config_manager.get_library_path(self.config):
            if ui_dialogs.ask_yes_no(
                self, "Choose a Music folder",
                "No Music folder is set yet, so there's nowhere to download the releases to. "
                "Choose one first?", "Choose folder", "Cancel"):
                self._on_change_folder()
            return
        dlg = ui_dialogs.DiscographyDialog(self, artist_id, self.config)
        if dlg.exec() != dlg.Accepted or dlg.info is None:
            return
        info = dlg.info
        artist_watch.remember_artist(self.config, info, dlg.watch, [r.album_id for r in info.releases])
        if dlg.picture:
            try:
                os.makedirs(ui_settings.ICONS_DIR, exist_ok=True)
                path = os.path.join(ui_settings.ICONS_DIR, f"artist_{info.artist_id}.jpg")
                with open(path, "wb") as f:
                    f.write(dlg.picture)
                ui_settings.set_icon(artist_key(info.artist_id), path)
            except OSError as e:
                logger.log_error(f"GUI: couldn't save the picture of '{info.name}': {e}")
        if self.search.text().strip() and not self._name_matches(info.name):
            self.search.clear()  # so the artist isn't hidden by the search
        queued = [i.name for i in self._adds or [] if i.state == "waiting"]
        names = artist_watch.plan_folder_names(self.config, info.name, dlg.chosen, taken=queued)
        datas = [{"mode": "download", "source": r.link, "name": names[r.album_id], "custom_path": None,
                  "artist_id": info.artist_id} for r in dlg.chosen]
        self.reload()
        if not datas:
            return
        if self._adds is not None:  # an Add run is going: they join its queue
            problems = []
            for data in datas:
                problem = self._queue_problem(data)
                if problem is None:
                    self._adds.append(QueuedAdd(data))
                else:
                    problems.append(f"{data['name']}: {problem}")
            self._refresh_queue_ui()
            if problems:
                ui_dialogs.warn(self, "Some releases couldn't be queued", "\n".join(problems))
            return
        first = None
        while datas and first is None:
            data = datas.pop(0)
            try:
                first = config_manager.add_playlist(self.config, data["source"], data["name"],
                                                    artist_id=data["artist_id"])
            except ValueError as e:
                ui_dialogs.warn(self, "Couldn't add a release", f"{data['name']}: {e}")
        if first is None:
            self.reload()
            return
        self.reload()
        self._start_add_run(first, data)
        if self._adds is not None:
            for rest in datas:
                self._adds.append(QueuedAdd(rest))
            self._refresh_queue_ui()

    def _apply_new_releases(self, artist_id: str, info) -> List[dict]:
        """41c, on the GUI thread for a running scan / sync (ui_backend.
        GuiCall): add the watched artist's new releases and show them; a
        Sync Now also lists them in its queue. Returns the new entries."""
        entries = artist_watch.add_new_releases(self.config, artist_id, info)
        if entries:
            if self.search.text().strip():
                self.search.clear()
            if self._syncs is not None:
                self._syncs.extend(QueuedSync(e) for e in entries)
                self.panel.bar.set_weights(bar_weights(self._syncs))  # v0.17.6
                self._refresh_sync_ui()
            if self._running_kind == "scan" and self._scan_control is not None:
                self._scan_total += len(entries)
                self.panel.bar.set_segments(self._scan_total)
        self.reload()
        return entries

    def _update_select_ui(self) -> None:
        if not self._mode:
            return
        self._sync_artist_ticks()
        rows = self._shown_rows()
        n = sum(1 for r in rows if r.is_checked())
        self.toggle_all_btn.setText("Uncheck all" if rows and n == len(rows) else "Check all")
        ticked = len(self._checked_entries())
        self.mode_go_btn.setEnabled(ticked > 0)
        # Ticks on playlists hidden by the search still count (v0.16.30): say so.
        hint = self._select_hint_base
        hidden = ticked - n
        if hidden:
            hint += (f"\n{hidden} ticked playlist{'s' if hidden != 1 else ''} hidden by the search "
                     f"will be included too.")
        self.select_hint.setText(hint)

    def _run_mode(self) -> None:
        mode, entries = self._mode, self._checked_entries()
        if not mode or not entries:
            return
        self._exit_mode()
        if mode == "sync":
            self._start_sync(entries)
        elif mode == "scan":
            self._start_scan(entries)
        elif mode == "copy":
            self._start_copy(entries)  # v0.26.0
        else:
            self._start_integrity(entries)
