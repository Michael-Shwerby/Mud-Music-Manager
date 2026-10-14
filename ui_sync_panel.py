"""
Mud Music Manager - Sync Progress panel (split out of ui_main_page.py in v0.23.4)

The panel along the bottom of the Main Menu while something runs
(SyncPanel), its Queue list (QueueRow) and the queue items of an Add run
(QueuedAdd) and of Sync Now (QueuedSync). See ui_main_page.py's docstring
and Design Doc §3.10 (Sync Progress panel).
"""

import time
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

import library_sync
import ui_settings
from ui_widgets import (
    CapsLabel, Drawer, DrawerSlide, ElidedLabel, GlyphButton, PillLabel, SegmentedProgressBar,
    Spinner, hseparator, make_button, smooth_scroll,
)
from ui_main_rows import playlist_link, short_link, _park_focus


def cached_track_count(entry: dict) -> Optional[int]:
    """The playlist's track count from its last Scan / Sync (v0.17.6), or
    None if it has never been read."""
    cache = ui_settings.get_cache(ui_settings.playlist_key(entry))
    n = cache.get("live_total") or cache.get("tracks")
    return n if isinstance(n, int) and n > 0 else None


def bar_weights(items) -> List[float]:
    """Sync Progress segment widths for these queue items (v0.17.6,
    Checklist item 57): each playlist's track count, so a big playlist
    takes more of the bar. One not read yet counts as the average of the
    known ones (all equal if none is known)."""
    known = [i.size for i in items if i.size]
    default = sum(known) / len(known) if known else 1.0
    return [float(i.size or default) for i in items]


class QueuedAdd:
    """One playlist of an Add run: the one being added now, one waiting in
    the queue, or one already done. Only lives in memory; a playlist is
    written to playlists.json (config_manager.add_playlist) when its turn
    comes, not when it is queued."""

    # state -> (pill text, pill tone)
    PILLS = {
        "waiting": ("Queued", "neutral"),
        "adding": ("Adding", "info"),
        "added": ("Added", "ok"),
        "sync_failed": ("Sync failed", "bad"),
        "stopped": ("Stopped", "warn"),
        "not_added": ("Not added", "bad"),
        "cancelled": ("Cancelled", "neutral"),
    }

    def __init__(self, data: dict):
        self.data = data            # AddPlaylistDialog.result_data
        self.state = "waiting"
        self.entry: Optional[dict] = None   # its playlists.json entry, once added
        self.run = None                     # its ui_backend.PlaylistRunResult, once synced
        self.text = ""                      # the line under its name
        self.size: Optional[int] = None     # v0.17.6: its track count, once read (bar segment width)

    @property
    def name(self) -> str:
        return self.data["name"]

    # the queue list's heading while the run goes on / after it
    LIVE_HEADING = "Adding playlists"
    DONE_HEADING = "Added this time"

    def describe(self) -> str:
        """What it is, for a playlist still waiting."""
        how = "Connect existing" if self.data.get("mode") == "connect" else "Download & sync"
        return f"{how} · {short_link(self.data.get('source', ''))}"

    def can_stop(self) -> bool:
        return self.state in ("waiting", "adding")

    def stop_tip(self) -> str:
        if self.state == "waiting":
            return "Take it out of the queue (it isn't added)"
        return "Stop adding this one and go on with the queue. It stays added; Sync Now finishes it."


class QueuedSync:
    """One playlist of a Sync Now run (v0.16.36, Checklist item 37): the
    one being synced, one waiting its turn, or one done. Shown in the same
    queue list as an Add run; only the one being synced has an ✕, which
    stops that playlist alone (the rest carry on)."""

    PILLS = {
        "waiting": ("Waiting", "neutral"),
        "syncing": ("Syncing", "info"),
        "synced": ("Synced", "ok"),
        "sync_failed": ("Sync failed", "bad"),
        "stopped": ("Stopped", "warn"),
        "not_synced": ("Not synced", "neutral"),
    }
    LIVE_HEADING = "Syncing playlists"
    DONE_HEADING = "Synced this time"

    def __init__(self, entry: dict):
        self.entry = entry
        self.key = ui_settings.playlist_key(entry)
        self.state = "waiting"
        self.stopping = False   # ✕ pressed; waiting for the current download
        self.run = None         # its ui_backend.PlaylistRunResult, once done
        self.text = ""
        # v0.17.6 (Checklist item 57): its track count, for the width of its
        # bar segment -- the cached one until the sync reads the real one.
        self.size: Optional[int] = cached_track_count(entry)

    @property
    def name(self) -> str:
        return self.entry.get("folder_name", "?")

    def describe(self) -> str:
        return f"Waiting its turn · {short_link(playlist_link(self.entry))}"

    def can_stop(self) -> bool:
        return self.state == "syncing" and not self.stopping

    def stop_tip(self) -> str:
        return ("Stop syncing this playlist only and go on with the next one (after the current "
                "download). What it downloaded is kept; Sync Now finishes it.")


class QueueRow(QWidget):
    """One line of the queue list: state pill, name, a line of detail and,
    while it can still be stopped, an ✕. v0.16.36: the item is a QueuedAdd
    (Add run) or a QueuedSync (Sync Now)."""

    cancelClicked = Signal(object)  # QueuedAdd / QueuedSync

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.item = item
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 2, 4)
        lay.setSpacing(10)
        self.pill = PillLabel("", "neutral")
        self.pill.setMinimumWidth(92)
        lay.addWidget(self.pill, 0, Qt.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.name = ElidedLabel(item.name)
        nf = self.name.font()
        nf.setBold(True)
        self.name.setFont(nf)
        text.addWidget(self.name)
        self.sub = ElidedLabel("")
        self.sub.setProperty("role", "muted")
        text.addWidget(self.sub)
        lay.addLayout(text, 1)
        self.cancel_btn = GlyphButton("close", 26)
        self.cancel_btn.clicked.connect(lambda: self.cancelClicked.emit(self.item))
        lay.addWidget(self.cancel_btn, 0, Qt.AlignVCenter)

    def refresh(self, live: bool) -> None:
        item = self.item
        text, tone = type(item).PILLS.get(item.state, ("", "neutral"))
        self.pill.setText(text)
        self.pill.set_tone(tone)
        sub = item.text or (item.describe() if item.state == "waiting" else "")
        self.sub.setText(sub)
        self.sub.setToolTip(sub)
        can_stop = live and item.can_stop()
        self.cancel_btn.setVisible(can_stop)
        self.cancel_btn.setToolTip(item.stop_tip())


# ---------------------------------------------------------------------------
# Sync Progress panel
# ---------------------------------------------------------------------------

class SyncPanel(QFrame):
    """Appears only while something is running (or to report that
    everything is already in sync). Pause turns into Resume; both Pause
    and Cancel wait for the current download to finish.

    v0.16.33 (Checklist item 34): while playlists are being added, a Queue
    button opens a list above the bar (the panel grows upward) with the one
    being added, the ones waiting (each with an ✕) and the ones done.

    v0.16.36 (Checklist item 37): Sync Now shows the same list, with an ✕
    on the playlist being synced that stops that one only."""

    pauseClicked = Signal()
    cancelClicked = Signal()
    detailsClicked = Signal()
    queueItemCancel = Signal(object)  # QueuedAdd / QueuedSync

    QUEUE_MAX_HEIGHT = 230

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("bar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._paused = False
        self._details_text = ""
        self._queue_items: Optional[List[QueuedAdd]] = None
        self._queue_live = False
        self._queue_open = False   # remembered between runs
        self._queue_rows: Dict[int, QueueRow] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 14, 20, 16)
        lay.setSpacing(10)

        # ---- the queue list, above the progress line (v0.16.33)
        self.queue_box = QWidget()
        qb = QVBoxLayout(self.queue_box)
        qb.setContentsMargins(0, 0, 0, 0)
        qb.setSpacing(6)
        self.queue_heading = CapsLabel("Adding playlists", muted=True)
        qb.addWidget(self.queue_heading)
        self.queue_scroll = QScrollArea()
        self.queue_scroll.setWidgetResizable(True)
        self.queue_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.queue_scroll.setFrameShape(QFrame.NoFrame)
        self.queue_host = QWidget()
        self.queue_layout = QVBoxLayout(self.queue_host)
        self.queue_layout.setContentsMargins(0, 0, 6, 0)
        self.queue_layout.setSpacing(2)
        self.queue_layout.addStretch(1)
        self.queue_scroll.setWidget(self.queue_host)
        smooth_scroll(self.queue_scroll)  # v0.17.0
        qb.addWidget(self.queue_scroll)
        qb.addWidget(hseparator())
        # v0.17.0: the list rises from behind the progress line (a Drawer)
        self.queue_drawer = Drawer(self.queue_box, "top")
        self._queue_slide = DrawerSlide(self.queue_drawer)
        lay.addWidget(self.queue_drawer)

        line = QHBoxLayout()
        line.setSpacing(12)
        self.spinner = Spinner(18)
        line.addWidget(self.spinner)
        self.title = QLabel("Sync Progress:")
        tf = self.title.font()
        tf.setPointSizeF(13.5)
        tf.setBold(True)
        self.title.setFont(tf)
        line.addWidget(self.title)
        self.output = ElidedLabel("")
        line.addWidget(self.output, 1)
        # v0.17.6 (Checklist item 57): a Sync's elapsed time and ETA
        self.timing = QLabel("")
        self.timing.setProperty("role", "muted")
        self.timing.setToolTip("Time spent syncing (pauses not counted) and an estimate of the time left. "
                               "The estimate uses this Sync's average time per download and per playlist, "
                               "blended with earlier Syncs'; “+” means a playlist still to come hasn't "
                               "been scanned, so its downloads aren't counted yet.")
        self.timing.hide()
        line.addWidget(self.timing)
        self._clock_start: Optional[float] = None   # monotonic; None: no Sync clock running
        self._clock_paused = 0.0
        self._paused_since: Optional[float] = None
        self._eta = None   # (elapsed when it came, seconds left, open)
        self._clock_tick = QTimer(self)
        self._clock_tick.setInterval(1000)
        self._clock_tick.timeout.connect(self._update_timing)
        self.queue_btn = make_button("Queue", compact=True)
        self.queue_btn.setToolTip("Show / hide the playlists of this run: the current one and the ones waiting")
        self.queue_btn.clicked.connect(self._toggle_queue)
        self.queue_btn.hide()
        line.addWidget(self.queue_btn)
        self.pause_btn = make_button("Pause", compact=True)
        self.pause_btn.clicked.connect(self.pauseClicked)
        self.cancel_btn = make_button("Cancel", "danger", compact=True)
        self.cancel_btn.clicked.connect(self.cancelClicked)
        self.details_btn = make_button("Details", compact=True)
        self.details_btn.clicked.connect(self.detailsClicked)
        self.dismiss_btn = make_button("Dismiss", compact=True)
        self.dismiss_btn.clicked.connect(self.hide_panel)
        for b in (self.pause_btn, self.cancel_btn, self.details_btn, self.dismiss_btn):
            line.addWidget(b)
        lay.addLayout(line)

        self.bar = SegmentedProgressBar()
        lay.addWidget(self.bar)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_panel)
        # v0.17.0: the panel rises into view from the bottom and sinks back,
        # at its full size, instead of the list above jumping by its height.
        # The page lays out `drawer` (which holds the panel), not the panel.
        self.drawer = Drawer(self, "top")
        self._slide = DrawerSlide(self.drawer)

    def _page(self) -> Optional[QWidget]:
        """The page the panel sits on (its drawer's parent), which takes the
        keyboard focus when a focused button here hides."""
        return self.drawer.parentWidget()

    def hide_panel(self) -> None:
        self._hide_timer.stop()
        self._clock_tick.stop()
        self.spinner.stop()
        _park_focus(self, self._page())
        self._slide.close()

    # ---- elapsed time and ETA (v0.17.6, Checklist item 57) -------------
    def start_clock(self) -> None:
        """A Sync (Sync Now or an Add run) starts: show its clock."""
        self._clock_start = time.monotonic()
        self._clock_paused = 0.0
        self._paused_since = None
        self._eta = None
        self.timing.show()
        self._clock_tick.start()
        self._update_timing()

    def stop_clock(self) -> Optional[float]:
        """The Sync ended: its active time in seconds (None if no clock ran)."""
        if self._clock_start is None:
            return None
        elapsed = self._elapsed()
        self._clock_start = None
        self._clock_tick.stop()
        return elapsed

    def _elapsed(self) -> float:
        now = time.monotonic()
        paused = self._clock_paused + ((now - self._paused_since) if self._paused_since is not None else 0.0)
        return max(0.0, now - self._clock_start - paused)

    def set_eta(self, seconds: Optional[float], open_: bool = False) -> None:
        """The latest estimate; it counts down between updates, and stands
        still while paused (it is measured in active time)."""
        if self._clock_start is None:
            return
        self._eta = None if seconds is None else (self._elapsed(), seconds, open_)
        self._update_timing()

    def _clock_pause(self, paused: bool) -> None:
        if self._clock_start is None:
            return
        now = time.monotonic()
        if paused and self._paused_since is None:
            self._paused_since = now
        elif not paused and self._paused_since is not None:
            self._clock_paused += now - self._paused_since
            self._paused_since = None
        self._update_timing()

    @staticmethod
    def _clock_text(seconds: float) -> str:
        seconds = int(seconds)
        h, rest = divmod(seconds, 3600)
        m, s = divmod(rest, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @staticmethod
    def _eta_text(seconds: float, open_: bool) -> str:
        if seconds < 60 and not open_:
            return "under a minute left"
        minutes = max(1, -(-int(seconds) // 60))  # rounded up
        h, m = divmod(minutes, 60)
        plus = "+" if open_ else ""  # "+": at least this long
        core = f"{h} h {m:02d}{plus} min" if h else f"{m}{plus} min"
        return f"{core} left" if open_ else f"~{core} left"

    def _update_timing(self) -> None:
        if self._clock_start is None:
            return
        elapsed = self._elapsed()
        text = f"Elapsed {self._clock_text(elapsed)}"
        if self._eta is None:
            text += "  ·  estimating time left…"
        else:
            at, seconds, open_ = self._eta
            text += "  ·  " + self._eta_text(max(0.0, seconds - (elapsed - at)), open_)
        self.timing.setText(text)

    def show_running(self, title: str, segments: int, pausable: bool, cancellable: bool, text: str = "",
                     cancel_text: str = "Cancel") -> None:
        self._hide_timer.stop()
        self._paused = False
        self.title.setText(title)
        self.output.setText(text)
        self._clock_start = None  # v0.17.6: a Sync starts its clock after this (start_clock)
        self._clock_tick.stop()
        self.timing.hide()
        self.bar.set_banner("")
        self.bar.set_segments(segments)
        self.bar.set_value(0)
        self.pause_btn.setText("Pause")
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setText(cancel_text)
        self.cancel_btn.setEnabled(True)
        self.pause_btn.setVisible(pausable)
        self.cancel_btn.setVisible(cancellable)
        self.details_btn.hide()
        self.dismiss_btn.hide()
        self.set_queue(None)  # an Add run / Sync Now sets it again
        self.spinner.start()
        self._slide.open()

    def reset_controls(self) -> None:
        """Pause / Cancel back to their starting state, for the next
        playlist of an Add run (v0.16.33): its own pause / cancel."""
        self._paused = False
        self.pause_btn.setText("Pause")
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.spinner.start()

    # ---- the queue list (v0.16.33) -------------------------------------
    def set_queue(self, items: Optional[list], live: bool = True) -> None:
        """Show these playlists (QueuedAdd or QueuedSync) in the queue list
        (None: no queue, the button and list hide). live=False once the run
        has ended: the list stays for reading, without ✕ buttons."""
        self._queue_items = items
        self._queue_live = live
        for row in self._queue_rows.values():
            _park_focus(row, self._page())
            self.queue_layout.removeWidget(row)
            row.hide()  # not drawn under the new lines until it is deleted
            row.deleteLater()
        self._queue_rows.clear()
        if items is None:
            self.queue_btn.hide()
            self._queue_slide.close(animate=False)
            return
        for item in items:
            row = QueueRow(item)
            row.cancelClicked.connect(self.queueItemCancel)
            row.refresh(live)
            self.queue_layout.insertWidget(self.queue_layout.count() - 1, row)
            self._queue_rows[id(item)] = row
        waiting = sum(1 for i in items if i.state == "waiting")
        kind = type(items[0]) if items else QueuedAdd
        self.queue_heading.setText(
            kind.LIVE_HEADING + (f"  ·  {waiting} waiting" if waiting else "") if live else kind.DONE_HEADING)
        self.queue_btn.show()
        self._fit_queue()
        self._update_queue_btn()

    def update_queue_item(self, item) -> None:
        """Just this playlist's line changed (progress text, state)."""
        row = self._queue_rows.get(id(item))
        if row is not None:
            row.refresh(self._queue_live)

    def queue_cancel_buttons(self) -> List[QWidget]:
        return [r.cancel_btn for r in self._queue_rows.values()]

    def _fit_queue(self) -> None:
        """The list is as tall as its rows, up to QUEUE_MAX_HEIGHT; then it scrolls."""
        rows = list(self._queue_rows.values())
        h = sum(r.sizeHint().height() for r in rows) + self.queue_layout.spacing() * max(0, len(rows) - 1)
        self.queue_scroll.setFixedHeight(max(36, min(h + 4, self.QUEUE_MAX_HEIGHT)))
        if self._queue_open and self._queue_items is not None:
            self._queue_slide.open(animate=False)
        else:
            self._queue_slide.close(animate=False)

    def _update_queue_btn(self) -> None:
        items = self._queue_items or []
        waiting = sum(1 for i in items if i.state == "waiting")
        label = f"Queue ({waiting})" if waiting else "Queue"
        # ▲ opens the list upward; ▼ folds it away again
        self.queue_btn.setText(f"{label}  {'▼' if self._queue_open else '▲'}")

    def _toggle_queue(self) -> None:
        self._queue_open = not self._queue_open
        if not self._queue_open:
            _park_focus(self.queue_box, self._page())
        if self._queue_open and self._queue_items is not None:
            self._queue_slide.open()  # v0.17.0: slides open / closed
        else:
            self._queue_slide.close()
        self._update_queue_btn()

    def update_progress(self, p) -> None:
        parts = [p.text] if p.text else []
        if p.track_total:
            parts.append(f"Track {p.track_index}/{p.track_total}")
        if p.playlist_total:
            parts.append(f"Playlist {p.playlist_index}/{p.playlist_total}")
        self.output.setText("  |  ".join(parts))
        self.bar.set_value(p.segments_value)
        self.set_eta(p.eta_seconds, p.eta_open)  # v0.17.6 (only while a Sync's clock runs)

    def set_text(self, text: str) -> None:
        self.output.setText(text)

    def set_bar(self, value: float) -> None:
        self.bar.set_value(value)

    def set_control_state(self, state: str) -> None:
        self._clock_pause(state == "paused")  # v0.17.6: time paused isn't counted
        if state == "pausing":
            self.output.setText("Pausing — finishing the current download…")
            self.pause_btn.setEnabled(False)
        elif state == "paused":
            self._paused = True
            self.output.setText("Paused — press Resume to continue.")
            self.pause_btn.setText("Resume")
            self.pause_btn.setEnabled(True)
            self.spinner.stop()
        elif state == "cancelling":
            self.output.setText("Cancelling — finishing the current download…")
            self.pause_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
        elif state == "running":
            self._paused = False
            self.pause_btn.setText("Pause")
            self.pause_btn.setEnabled(True)
            self.spinner.start()

    def show_finished(self, text: str, details: str = "", auto_hide_ms: int = 0,
                      banner: str = "") -> None:
        """banner: large text shown inside the progress bar (e.g. 'Sync
        Complete'); empty keeps the thin bar. v0.17.6: after a Sync, the
        clock's place shows how long it took."""
        self.spinner.stop()
        elapsed = self.stop_clock()
        if elapsed is None:
            self.timing.hide()
        else:
            self.timing.setText(f"Took {library_sync.format_duration(elapsed)}")
            self.timing.show()
        self.output.setText(text)
        self.bar.set_banner(banner)
        self.pause_btn.hide()
        self.cancel_btn.hide()
        self._details_text = details
        self.details_btn.setVisible(bool(details))
        self.dismiss_btn.show()
        self._slide.open()
        if auto_hide_ms:
            self._hide_timer.start(auto_hide_ms)


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------
