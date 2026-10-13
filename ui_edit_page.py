"""
Mud Music Manager - Edit Screen (v0.16.0)

Manages one playlist "on a micro level": rename it, change its icon,
change the playlist's own link, see every song, play the local files,
and add / change / remove Exceptions for individual songs. Reads the
local files only (works offline). Nothing is applied until Save; Back
with unsaved changes asks whether to save or discard.

The Exception URL IS the exception: the box under "Selected Track" only
appears for a song that has one, and editing it changes that song's
exception.

Playlist link (v0.16.12, Checklist item 16): the field under the name
may point the playlist at a different Spotify or YouTube playlist,
including switching platform. Because the next sync then removes the
folder's current songs, Save first shows a warning that requires typing
the playlist's name (ConfirmLinkChangeDialog). A link change can't be
saved together with exception changes, which belong to the current
playlist's songs.

Keyboard control (v0.16.23, Checklist item 27 part 2): W/S choose a song,
Q plays it, Space plays / pauses, A/D skip 5 s, C/V volume, R/T previous
/ next track, E exception, 1/2/3 icon / name / link, F logs, Ctrl+S Save,
Esc Back (keymap in ui_keys.py). Esc while typing in a field puts back
what was there and leaves it.

Search bar (v0.16.31, Checklist item 32): shows only the songs whose title,
artist or album contains every word typed; a number also matches the
track number, and "#12" matches track 12 only. Ctrl+F or / types in it,
Enter selects the first song shown, Esc on the page clears it. It only
hides rows: the selection, the player's order and the Exceptions list
stay as they are. v0.16.32: clicking a song leaves the search bar (so the
keys act on that song), and clearing the search scrolls the full list to
the selected song.

Unavailable on Spotify (v0.16.39, Checklist items 45 / 46): an item Spotify
lists but gives no details for (e.g. not available in this country) shows
as a greyed, dashed spacer row at its Track #, reading "Unavailable on
Spotify" and Spotify's reason, and the header has a neutral "N unavailable
on Spotify" pill. Both come from the last Scan / Sync (cached). A spacer
can be selected but has no exception and can't be played.

Own files (v0.16.40, Checklist item 47): any song, a spacer included, can
use an audio file the user already has ("Use your own file…" in the
Selected Track box and the Exception dialog, the spacer's Import button,
or I). ImportFileDialog confirms it (a spacer's title / artist are typed,
pre-filled from the file's tags). Like exceptions it stays pending until
Save, which runs library_sync.import_track(). Songs with their own file
show "Own file" on their row button and in the Exceptions list. A new
exception link and an import for the same song replace each other.

Two lists (v0.16.43, Checklist item 38): the right column's table of
contents is split into Exceptions (links and own files, as before) and
Issues / Pending (every song not downloaded, with its status, and the
unavailable-on-Spotify spacers). Each opens / closes from its header
(TocSection); which are closed is remembered in ui_settings.json
("edit_sections_collapsed"). Clicking an entry selects its song in both.

v0.17.0: the song list and both side lists scroll smoothly (wheel and
keys), and the two lists open / close with a gliding motion while their
arrow turns; the Selected Track box is bottom-aligned so it never moves.
"""

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QSizePolicy, QSlider,
    QVBoxLayout, QWidget,
)

try:  # the media backend can be missing on a stripped-down install
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    HAVE_MEDIA = True
except Exception:  # pragma: no cover
    HAVE_MEDIA = False

import config_manager
import exceptions_manager
import library_sync
import logger
import metadata_tagger
import track_failures
import ui_backend
import ui_dialogs
import ui_keys
import ui_settings
from ui_main_page import playlist_link, source_link
from ui_widgets import (
    ANIM_CLOSE_MS, ANIM_OPEN_MS, MAX_HEIGHT, CapsLabel, ElidedLabel, GlyphButton, PillLabel, SpeakerIcon,
    ThumbLabel, animations_on, ensure_visible, format_duration, make_button, make_panel, make_value_anim,
    motion_curve, run_value_anim, scroll_area_to, smooth_scroll,
)

# Player (v0.16.9): width shared by the track-info and volume columns, so
# the controls between them are centred; starting volume in percent
# (the old fixed setVolume(0.85)).
PLAYER_SIDE_WIDTH = 280
DEFAULT_VOLUME = 85

# Keyboard (v0.16.23): A/D skip, C/V volume step.
SEEK_STEP_MS = 5000
VOLUME_STEP = 5


def _repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


@dataclass
class Row:
    """One song as the Edit page shows it: a downloaded file, or (path
    None) a live track that hasn't been downloaded yet."""
    key: str
    number: int
    title: str
    artist: str
    album: str
    seconds: float
    path: Optional[str] = None
    pixmap: Optional[QPixmap] = None
    failure: Optional[dict] = None   # track_failures record of a not-downloaded song (v0.16.27)
    # v0.16.39 (Checklist item 46): set (Spotify's reason, in words) on a
    # spacer row for an item Spotify gives no details for.
    unavailable: Optional[str] = None
    # v0.16.40 (Checklist item 47): the original file name of a song
    # imported from the user's own file; a spacer's Spotify ID (an import
    # fills that slot).
    own_file: Optional[str] = None
    spotify_id: str = ""
    # v0.16.48 (Checklist item 41a): disc number on a multi-disc album
    # (0 otherwise); rows are ordered by disc, then Track #.
    disc: int = 0


def _num_text(row: Row) -> str:
    """The Track # shown for a song: "2-1" for disc 2, track 1 of a
    multi-disc album (v0.16.48), else the number."""
    if not row.number:
        return "–"
    return f"{row.disc}-{row.number}" if row.disc else str(row.number)


# Spacer rows' keys (v0.16.39): never a Spotify ID, so no exception or
# file can ever be tied to one by mistake.
UNAVAILABLE_KEY_PREFIX = "spotify-unavailable:"
UNAVAILABLE_TITLE = "Unavailable on Spotify"


def _unavailable_details(row: Row) -> str:
    """Selected Track box / tooltip text for a spacer row."""
    return (f"{UNAVAILABLE_TITLE}: {row.unavailable}.\n"
            f"Spotify lists this song at #{row.number} but gives no details for it (no title, "
            f"artist or length), so it can't be searched for or downloaded. It keeps its place, "
            f"so the songs after it have the same Track # as on Spotify. Not counted as pending.")


def _failure_display(row: Row):
    """(text, role, tooltip) for a not-downloaded song (v0.16.27,
    Checklist item 28): "No match" / "Unavailable" in red, the kind of
    the last failure in amber while it is still being retried, or "Not
    downloaded" when it never failed."""
    record = row.failure
    if not record:
        return "Not downloaded", "warn", "Not downloaded yet. The next sync downloads it."
    st = track_failures.status(record)
    kind = record.get("kind")
    if st == track_failures.STATUS_NO_MATCH:
        text, role = "No match", "bad"
    elif kind == track_failures.AGE_RESTRICTED:  # v0.16.38: red at once, under its own name
        text, role = track_failures.label(kind), "bad"
    elif st == track_failures.STATUS_UNAVAILABLE:
        text, role = "Unavailable", "bad"
    else:
        text, role = track_failures.label(kind), "warn"
    return text, role, _failure_details(record, text)


def _failure_details(record: dict, shown: str) -> str:
    """The longer explanation (row tooltip, Selected Track box): what
    happened, how often, and what to do about it."""
    kind = record.get("kind")
    label = track_failures.label(kind)
    heading = shown if shown == label else f"{shown} — {label}"
    failed = int(record.get("failed_syncs") or 0)
    if kind in (track_failures.NO_MATCH, track_failures.AGE_RESTRICTED):
        count = ""
    elif track_failures.status(record) == track_failures.STATUS_UNAVAILABLE:
        count = f"Failed on {failed} syncs (still tried on every sync)."
    elif kind in track_failures.NOT_COUNTED:
        count = f"Not counted as a failed attempt ({failed} of {track_failures.UNAVAILABLE_AFTER} so far)."
    else:
        count = f"Failed on {failed} of {track_failures.UNAVAILABLE_AFTER} syncs before it shows as Unavailable."
    lines = [heading, record.get("reason") or "unknown reason"]
    if count:
        lines.append(count)
    lines.append(track_failures.hint(kind))
    lines.append(f"Last failed: {record.get('last_failed', '?')}")
    return "\n".join(lines)


def _video_id(link: str) -> Optional[str]:
    return exceptions_manager.extract_youtube_id(link.strip()) if link else None


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _platform_label(platform: str, kind: str = config_manager.KIND_PLAYLIST) -> str:
    return config_manager.source_label(platform, kind)  # v0.16.48: "Spotify album" too


# ---------------------------------------------------------------------------
# One song in the list
# ---------------------------------------------------------------------------

class TrackRow(QFrame):
    selected = Signal(str)        # key
    playRequested = Signal(str)   # key
    exceptionRequested = Signal(str)
    importRequested = Signal(str)  # v0.16.40: a spacer's Import button

    def __init__(self, row: Row, parent=None):
        super().__init__(parent)
        self.row = row
        self.key = row.key
        self.setObjectName("trow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setProperty("selected", False)
        self.setProperty("playing", False)
        self.setMouseTracking(True)
        self.has_exception = False
        missing = row.path is None
        # v0.16.39 (Checklist item 46): a greyed spacer for an item Spotify
        # gives no details for, at its Track #. No exception button.
        self.spacer = row.unavailable is not None
        self.setProperty("spacer", self.spacer)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(12)

        self.thumb = ThumbLabel(42, 0.18, overlay=None if missing else "play")
        self.thumb.set_placeholder_name(row.album or row.title)
        if row.pixmap:
            self.thumb.set_pixmap(row.pixmap)
        self.thumb.clicked.connect(lambda: self.playRequested.emit(self.key))
        lay.addWidget(self.thumb)

        self.num = QLabel(_num_text(row))
        self.num.setFixedWidth(40 if row.disc else 30)
        self.num.setAlignment(Qt.AlignCenter)
        nf = self.num.font()
        nf.setBold(True)
        self.num.setFont(nf)
        lay.addWidget(self.num)

        names = QVBoxLayout()
        names.setSpacing(0)
        self.title = ElidedLabel(row.title)
        tf = self.title.font()
        tf.setBold(True)
        self.title.setFont(tf)
        self.artist = ElidedLabel(row.artist)
        self.artist.setProperty("role", "muted")
        names.addWidget(self.title)
        names.addWidget(self.artist)
        lay.addLayout(names, 5)

        self.album = ElidedLabel(row.album)
        self.album.setProperty("role", "muted")
        lay.addWidget(self.album, 4)

        if self.spacer:
            for label in (self.num, self.title, self.artist):
                label.setProperty("role", "faint")
            self.length = QLabel("Unavailable")
            self.length.setProperty("role", "faint")
            self.setToolTip(_unavailable_details(row))
        elif missing:
            text, role, tip = _failure_display(row)
            self.length = QLabel(text)
            self.length.setProperty("role", role)
            self.length.setToolTip(tip)
        else:
            self.length = QLabel(format_duration(row.seconds))
            self.length.setProperty("role", "muted")
        self.length.setFixedWidth(124)
        lay.addWidget(self.length)

        cell = QWidget()
        cell.setFixedWidth(112)
        cl = QHBoxLayout(cell)
        cl.setContentsMargins(0, 0, 0, 0)
        # v0.16.40 (Checklist item 47): on a spacer this is "Import" (use your
        # own file); a song with an own file (saved or pending) shows "Own file".
        self.exc_btn = make_button("Import" if self.spacer else "Exception", compact=True)
        self.exc_btn.clicked.connect(
            lambda: (self.importRequested if self.spacer else self.exceptionRequested).emit(self.key))
        self.exc_btn.hide()
        cl.addWidget(self.exc_btn)
        lay.addWidget(cell)

    def set_exception(self, on: bool, own_file: bool = False) -> None:
        self.has_exception = on or own_file
        if own_file:
            text = "Own file"
        elif self.spacer:
            text = "Import"
        else:
            text = "1 Exception" if on else "Exception"
        self.exc_btn.setProperty("variant", "accent" if self.has_exception else "")
        self.exc_btn.setText(text)
        _repolish(self.exc_btn)
        self.exc_btn.setVisible(self.has_exception or self.underMouse())

    def set_selected(self, on: bool) -> None:
        self.setProperty("selected", on)
        _repolish(self)

    def set_playing(self, on: bool) -> None:
        self.setProperty("playing", on)
        _repolish(self)

    def enterEvent(self, e):
        self.exc_btn.setVisible(True)

    def leaveEvent(self, e):
        self.exc_btn.setVisible(self.has_exception)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.selected.emit(self.key)

    def mouseDoubleClickEvent(self, e):
        if self.row.path:
            self.playRequested.emit(self.key)


class TocEntry(QFrame):
    clicked = Signal(str)

    def __init__(self, row: Row, subtitle: str = "Fixed With New Link", title: str = "", parent=None,
                 subtitle_role: str = "muted", tooltip: str = ""):
        super().__init__(parent)
        self.key = row.key
        self.setObjectName("trow")
        if tooltip:
            self.setToolTip(tooltip)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)
        th = ThumbLabel(36, 0.18)
        th.set_placeholder_name(row.album or row.title)
        if row.pixmap:
            th.set_pixmap(row.pixmap)
        lay.addWidget(th)
        n = QLabel(_num_text(row))
        nf = n.font()
        nf.setBold(True)
        nf.setPointSizeF(12)
        n.setFont(nf)
        lay.addWidget(n)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = ElidedLabel(title or row.title)
        tf = t.font()
        tf.setBold(True)
        t.setFont(tf)
        sub = QLabel(subtitle)
        sub.setProperty("role", subtitle_role)
        col.addWidget(t)
        col.addWidget(sub)
        lay.addLayout(col, 1)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.key)


class _SectionHeader(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class TocSection(QFrame):
    """A collapsible list in the Edit page's right column (v0.16.43,
    Checklist item 38): a header (chevron, title, count pill) that opens /
    closes it on a click, and a scrolling list of TocEntry rows with a
    line of text when it is empty. Used for Exceptions and Issues / Pending."""
    toggled = Signal(bool)  # collapsed
    entryClicked = Signal(str)  # key

    def __init__(self, title: str, empty_text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.collapsed = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 12, 12)
        lay.setSpacing(8)
        self.header = _SectionHeader()
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        t = QLabel(title)
        tf = t.font()
        tf.setPointSizeF(16)
        tf.setBold(True)
        t.setFont(tf)
        hl.addWidget(t)
        self.count = PillLabel("0", "neutral")
        hl.addWidget(self.count)
        hl.addStretch(1)
        # v0.17.0: one arrow that turns (pointing up = open), as on the
        # Main Menu's bars; the page turns it as the list opens / closes
        self.chevron = GlyphButton("chev_down", 28)
        self.chevron.clicked.connect(self.toggle)
        hl.addWidget(self.chevron)
        self.header.clicked.connect(self.toggle)
        lay.addWidget(self.header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        self.list_layout = QVBoxLayout(host)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(host)
        smooth_scroll(self.scroll)  # v0.17.0
        lay.addWidget(self.scroll, 1)
        self.empty = QLabel(empty_text)
        self.empty.setProperty("role", "muted")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.list_layout.insertWidget(0, self.empty)

    def set_entries(self, entries: List[TocEntry], tone: str) -> None:
        """Replace the list; the count pill shows how many, in `tone`
        (neutral when there are none)."""
        for i in reversed(range(self.list_layout.count())):
            wid = self.list_layout.itemAt(i).widget()
            if isinstance(wid, TocEntry):
                self.list_layout.removeWidget(wid)
                wid.deleteLater()
        for e in entries:
            e.clicked.connect(self.entryClicked.emit)
            self.list_layout.insertWidget(self.list_layout.count() - 1, e)
        self.empty.setVisible(not entries)
        self.count.setText(str(len(entries)))
        self.count.set_tone(tone if entries else "neutral")

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.scroll.setVisible(not collapsed)
        self.chevron.set_rotation(0 if collapsed else 180)
        self.chevron.setToolTip("Show the list" if collapsed else "Hide the list")
        # Collapsed, only the header row: never take spare height.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum if collapsed else QSizePolicy.Preferred)

    def toggle(self) -> None:
        self.set_collapsed(not self.collapsed)
        self.toggled.emit(self.collapsed)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

class EditPage(QWidget):
    backRequested = Signal()
    dirtyChanged = Signal(bool)

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.entry: Optional[dict] = None
        self.folder: Optional[str] = None
        self._rows: List[Row] = []
        self._widgets: Dict[str, TrackRow] = {}
        self._saved_exceptions: Dict[str, str] = {}
        self._pending: Dict[str, Optional[str]] = {}   # key -> video id (None = removed)
        # v0.16.40 (Checklist item 47): key -> the user's audio file to import
        # for that song, pending until Save like the exceptions.
        self._imports: Dict[str, ui_backend.ImportChoice] = {}
        self._import_dir = ""  # where the file picker opens next (this session)
        self._orig_name = ""
        self._pending_icon: Optional[QPixmap] = None
        self._selected: Optional[str] = None
        self._playing_key: Optional[str] = None
        self._queue: List[str] = []
        self._loading = False
        self._seeking = False
        # Keyboard (v0.16.23): a field's text when the typing cursor
        # entered it, put back by Esc.
        self._field_before: Dict[QLineEdit, str] = {}
        # Search (v0.16.31): the list's text when no song is shown and no
        # search is the reason ("Loading songs…", "no songs yet"…).
        self._empty_text = ""
        # v0.16.32: the search the list was last filtered by, to notice it
        # being cleared.
        self._last_query = ""
        # v0.16.38: the exception video the Selected Track box last checked.
        self._checked_vid: Optional[str] = None

        # ---- header buttons (the app shell places these in its top bar)
        self.save_btn = make_button("Save", "primary")
        self.save_btn.clicked.connect(self.save)
        self.save_btn.setEnabled(False)
        self.back_btn = make_button("←  Back")
        self.back_btn.clicked.connect(self.request_back)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 4, 26, 22)
        root.setSpacing(16)

        mid = QHBoxLayout()
        mid.setSpacing(16)
        root.addLayout(mid, 1)

        # ================= songs panel ================================
        songs = make_panel()
        sl = QVBoxLayout(songs)
        sl.setContentsMargins(20, 16, 12, 12)
        sl.setSpacing(10)
        mid.addWidget(songs, 5)

        head = QHBoxLayout()
        head.setSpacing(14)
        self.icon = ThumbLabel(76, 0.2, overlay="edit")
        self.icon.setToolTip("Change playlist image")
        self.icon.clicked.connect(self._change_icon)
        head.addWidget(self.icon)
        hcol = QVBoxLayout()
        hcol.setSpacing(4)
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self.position_label = QLabel("1.")
        pf = self.position_label.font()
        pf.setPointSizeF(21)
        pf.setBold(True)
        self.position_label.setFont(pf)
        self.position_label.setProperty("role", "muted")
        name_row.addWidget(self.position_label)
        self.name_edit = QLineEdit()
        self.name_edit.setProperty("flat", True)
        nf = self.name_edit.font()
        nf.setPointSizeF(21)
        nf.setBold(True)
        self.name_edit.setFont(nf)
        self.name_edit.textChanged.connect(self._on_name_changed)
        name_row.addWidget(self.name_edit, 1)
        hcol.addLayout(name_row)
        pills = QHBoxLayout()
        pills.setSpacing(8)
        self.status_pill = PillLabel("Not checked", "neutral")
        self.exc_pill = PillLabel("0 Exceptions", "neutral")
        # v0.16.39 (Checklist item 45): "1 unavailable on Spotify"; hidden
        # when there are none. Shown only here since v0.16.41.
        self.unavail_pill = PillLabel("", "neutral")
        self.unavail_pill.hide()
        pills.addWidget(self.status_pill)
        pills.addWidget(self.exc_pill)
        pills.addWidget(self.unavail_pill)
        pills.addStretch(1)
        hcol.addLayout(pills)
        # The playlist's own link (Checklist item 16): pending until Save.
        link_row = QHBoxLayout()
        link_row.setSpacing(8)
        link_cap = QLabel("Link")
        link_cap.setProperty("role", "muted")
        link_row.addWidget(link_cap)
        self.link_edit = QLineEdit()
        self.link_edit.setPlaceholderText("Spotify, YT Music, Apple Music, Amazon Music, SoundCloud or YouTube playlist or album link")
        self.link_edit.textChanged.connect(self._on_link_changed)
        self.link_edit.editingFinished.connect(self._link_edited)
        link_row.addWidget(self.link_edit, 1)
        hcol.addLayout(link_row)
        self.link_hint = QLabel("")
        self.link_hint.setWordWrap(True)
        self.link_hint.hide()
        hcol.addWidget(self.link_hint)
        head.addLayout(hcol, 1)
        self.logs_btn = make_button("View\nSync Logs")
        self.logs_btn.clicked.connect(self._view_logs)
        head.addWidget(self.logs_btn, 0, Qt.AlignTop)
        sl.addLayout(head)

        # v0.16.31 (Checklist item 32): show only the songs that match,
        # like the Main Menu's playlist search. Not saved; not a change
        # that needs Save.
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search songs")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Show only the songs whose title, artist or album contains every word typed.\n"
                               "A number also finds that track number; #12 finds track 12 only.")
        self.search.setMinimumWidth(160)
        self.search.setMaximumWidth(340)
        self.search.textChanged.connect(self._apply_filter)
        self.search.returnPressed.connect(self._search_entered)
        search_row.addWidget(self.search, 3)
        search_row.addStretch(1)
        self.search_count = QLabel("")
        self.search_count.setProperty("role", "muted")
        search_row.addWidget(self.search_count)
        sl.addLayout(search_row)

        cols = QHBoxLayout()
        cols.setContentsMargins(8, 4, 20, 0)
        cols.setSpacing(12)
        spacer = QWidget()
        spacer.setFixedWidth(42)
        cols.addWidget(spacer)
        for text, w in (("#", 30),):
            c = CapsLabel(text, muted=True)
            c.setFixedWidth(w)
            c.setAlignment(Qt.AlignCenter)
            cols.addWidget(c)
        cols.addWidget(CapsLabel("Title", muted=True), 5)
        cols.addWidget(CapsLabel("Album", muted=True), 4)
        ln = CapsLabel("Length", muted=True)
        ln.setFixedWidth(124)  # matches TrackRow.length (wider since v0.16.27 for the failure labels)
        cols.addWidget(ln)
        ex = CapsLabel("Exception", muted=True)
        ex.setFixedWidth(112)
        cols.addWidget(ex)
        sl.addLayout(cols)
        sep = QFrame()
        sep.setObjectName("sep")
        sep.setFixedHeight(1)
        sl.addWidget(sep)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        self.rows_layout = QVBoxLayout(host)
        self.rows_layout.setContentsMargins(0, 2, 6, 2)
        self.rows_layout.setSpacing(2)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(host)
        smooth_scroll(self.scroll)  # v0.17.0
        sl.addWidget(self.scroll, 1)

        self.empty_label = QLabel("")
        self.empty_label.setProperty("role", "muted")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.rows_layout.insertWidget(0, self.empty_label)

        # ================= right column ================================
        right = QVBoxLayout()
        right.setSpacing(16)
        right_host = QWidget()
        right_host.setFixedWidth(372)
        right_host.setLayout(right)
        mid.addWidget(right_host, 0)

        # v0.16.43 (Checklist item 38): two collapsible lists, Exceptions
        # (links and own files) and Issues / Pending (songs not downloaded,
        # spacers). Open / closed is remembered between launches.
        self._right = right
        self.exc_section = TocSection("Exceptions", "No exceptions yet.\nHover a song and press “Exception” "
                                      "to add one.")
        self.issue_section = TocSection("Issues / Pending", "Nothing pending.\nEvery song from the last "
                                        "Scan or Sync is downloaded.")
        self.issue_section.setToolTip("Songs that aren't downloaded yet, as of the last Scan or Sync")
        for name, section in (("exceptions", self.exc_section), ("issues", self.issue_section)):
            section.entryClicked.connect(self._toc_clicked)
            section.set_collapsed(bool(self._collapsed_sections().get(name)))
            section.toggled.connect(lambda collapsed, n=name: self._section_toggled(n, collapsed))
            right.addWidget(section, 1)
        # v0.17.0: the lists open / close with the drawer motion. Their
        # heights glide together (the same curve as the Main Menu's bars).
        self._sec_anim = make_value_anim(self, self._sections_step, ANIM_OPEN_MS, motion_curve())
        self._sec_anim.finished.connect(self._finish_section_anim)
        self._sec_moves: list = []   # (section, from height, to height)
        self._sec_turn = None        # (section, from degrees, to degrees)
        self._fit_sections()

        sel = make_panel()
        sv = QVBoxLayout(sel)
        sv.setContentsMargins(20, 16, 20, 16)
        sv.setSpacing(10)
        st = QLabel("Selected Track")
        stf = st.font()
        stf.setPointSizeF(15)
        stf.setBold(True)
        st.setFont(stf)
        sv.addWidget(st)
        sel_row = QHBoxLayout()
        sel_row.setSpacing(12)
        self.sel_thumb = ThumbLabel(64, 0.2)
        sel_row.addWidget(self.sel_thumb)
        self.sel_num = QLabel("")
        snf = self.sel_num.font()
        snf.setPointSizeF(20)
        snf.setBold(True)
        self.sel_num.setFont(snf)
        sel_row.addWidget(self.sel_num)
        sc = QVBoxLayout()
        sc.setSpacing(0)
        self.sel_title = ElidedLabel("Select a song")
        sttf = self.sel_title.font()
        sttf.setPointSizeF(14)
        sttf.setBold(True)
        self.sel_title.setFont(sttf)
        self.sel_artist = ElidedLabel("")
        self.sel_artist.setProperty("role", "muted")
        sc.addStretch(1)
        sc.addWidget(self.sel_title)
        sc.addWidget(self.sel_artist)
        sc.addStretch(1)
        sel_row.addLayout(sc, 1)
        sv.addLayout(sel_row)
        # v0.16.27 (Checklist item 28): why a not-downloaded song failed.
        self.sel_failure = QLabel("")
        self.sel_failure.setWordWrap(True)
        self.sel_failure.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.sel_failure.hide()
        sv.addWidget(self.sel_failure)
        self.url_box = QWidget()
        ub = QVBoxLayout(self.url_box)
        ub.setContentsMargins(0, 0, 0, 0)
        ub.setSpacing(4)
        ub.addWidget(QLabel("Exception URL:"))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Paste a YouTube link or video ID")
        self.url_edit.editingFinished.connect(self._url_edited)
        ub.addWidget(self.url_edit)
        self.url_hint = QLabel("")
        self.url_hint.setProperty("role", "bad")
        self.url_hint.setWordWrap(True)
        ub.addWidget(self.url_hint)
        # v0.16.38 (Checklist item 44): says whether the exception video
        # can be downloaded (age-restricted, private, removed...).
        self._link_checker = ui_dialogs.LinkChecker(self)
        self._link_checker.result.connect(self._on_link_checked)
        self.url_box.hide()
        sv.addWidget(self.url_box)
        # v0.16.40 (Checklist item 47): use your own audio file for the song.
        imp_row = QHBoxLayout()
        imp_row.setSpacing(8)
        self.import_btn = make_button("Use your own file…", compact=True)
        self.import_btn.setToolTip("Use an audio file you already have for this song (applied on Save)")
        self.import_btn.clicked.connect(lambda: self._selected and self._import_for(self._selected))
        self.cancel_import_btn = make_button("Cancel import", compact=True)
        self.cancel_import_btn.clicked.connect(lambda: self._selected and self._cancel_import(self._selected))
        imp_row.addWidget(self.import_btn)
        imp_row.addWidget(self.cancel_import_btn)
        imp_row.addStretch(1)
        self.import_box = QWidget()
        self.import_box.setLayout(imp_row)
        imp_row.setContentsMargins(0, 0, 0, 0)
        self.import_box.hide()
        sv.addWidget(self.import_box)
        # Bottom-aligned (v0.17.0): with both lists closed the spare height
        # sits above it, so it stays at the bottom without the old filler
        # widget, and it never moves while the lists slide.
        right.addWidget(sel, 0, Qt.AlignBottom)

        # ================= player ======================================
        # Three columns: track info | controls + slider | volume. The two
        # side columns share one fixed width and the panel margins are
        # equal, so the controls sit in the exact centre of the panel
        # (v0.16.9, Checklist item 13).
        player = make_panel()
        pl = QHBoxLayout(player)
        pl.setContentsMargins(20, 12, 20, 12)
        pl.setSpacing(16)

        info = QWidget()
        info.setFixedWidth(PLAYER_SIDE_WIDTH)
        il = QHBoxLayout(info)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(14)
        self.pl_thumb = ThumbLabel(56, 0.2)
        il.addWidget(self.pl_thumb)
        pc = QVBoxLayout()
        pc.setSpacing(0)
        pc.addStretch(1)
        self.pl_title = ElidedLabel("Nothing playing")
        ptf = self.pl_title.font()
        ptf.setPointSizeF(13)
        ptf.setBold(True)
        self.pl_title.setFont(ptf)
        self.pl_artist = ElidedLabel("Click a song’s picture to play it")
        self.pl_artist.setProperty("role", "muted")
        pc.addWidget(self.pl_title)
        pc.addWidget(self.pl_artist)
        pc.addStretch(1)
        il.addLayout(pc, 1)
        pl.addWidget(info)

        mid_col = QVBoxLayout()
        mid_col.setSpacing(4)
        ctl = QHBoxLayout()
        ctl.setSpacing(14)
        ctl.addStretch(1)
        self.prev_btn = GlyphButton("prev", 40, ring=True)
        self.play_btn = GlyphButton("play", 52, ring=True)
        self.next_btn = GlyphButton("next", 40, ring=True)
        self.prev_btn.clicked.connect(self._prev)
        self.play_btn.clicked.connect(self._toggle_play)
        self.next_btn.clicked.connect(self._next)
        ctl.addWidget(self.prev_btn)
        ctl.addWidget(self.play_btn)
        ctl.addWidget(self.next_btn)
        ctl.addStretch(1)
        mid_col.addLayout(ctl)
        scrub = QHBoxLayout()
        scrub.setSpacing(10)
        self.time_now = QLabel("0:00")
        self.time_now.setProperty("role", "muted")
        self.time_total = QLabel("0:00")
        self.time_total.setProperty("role", "muted")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setMinimumWidth(260)
        self.slider.sliderPressed.connect(self._seek_begin)
        self.slider.sliderReleased.connect(self._seek_end)
        scrub.addWidget(self.time_now)
        scrub.addWidget(self.slider, 1)
        scrub.addWidget(self.time_total)
        mid_col.addLayout(scrub)
        pl.addLayout(mid_col, 1)

        vol = QWidget()
        vol.setFixedWidth(PLAYER_SIDE_WIDTH)
        vl = QHBoxLayout(vol)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(8)
        vl.addStretch(1)
        self.vol_icon = SpeakerIcon(22)
        vl.addWidget(self.vol_icon)
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setFixedWidth(140)
        self.vol_slider.setPageStep(10)
        self.vol_slider.valueChanged.connect(self._on_volume)
        vl.addWidget(self.vol_slider)
        pl.addWidget(vol)
        root.addWidget(player)

        self.player = None
        self.audio = None
        if HAVE_MEDIA:
            try:
                self.player = QMediaPlayer(self)
                self.audio = QAudioOutput(self)
                self.audio.setVolume(DEFAULT_VOLUME / 100)
                self.player.setAudioOutput(self.audio)
                self.player.positionChanged.connect(self._on_position)
                self.player.durationChanged.connect(self._on_duration)
                self.player.mediaStatusChanged.connect(self._on_media_status)
                self.player.playbackStateChanged.connect(self._on_state)
                self.player.errorOccurred.connect(self._on_player_error)
            except Exception as e:  # pragma: no cover
                logger.log_error(f"GUI: media player unavailable: {e}")
                self.player = None
        # The volume works in every player state (set it before pressing
        # play); it is only disabled when there is no audio backend.
        self.vol_slider.setValue(DEFAULT_VOLUME)
        self._on_volume(DEFAULT_VOLUME)
        self.vol_slider.setEnabled(self.player is not None)
        self.vol_icon.setEnabled(self.player is not None)
        self._set_player_empty()

        for field in (self.name_edit, self.link_edit, self.url_edit, self.search):
            field.installEventFilter(self)

    # ------------------------------------------------------------------
    # Opening / loading
    # ------------------------------------------------------------------
    def header_widgets(self):
        return [self.save_btn, self.back_btn]

    @property
    def config(self) -> dict:
        return self.ctx.config

    def open_entry(self, entry: dict, keep_search: bool = False) -> None:
        """Show this playlist. Opening one from the Main Menu starts with no
        search; reopening after Save (keep_search) keeps it."""
        self.stop_player()
        self.entry = entry
        if not keep_search:
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            self._last_query = ""
        self._pending = {}
        self._imports = {}
        self._pending_icon = None
        self._selected = None
        self._orig_name = entry["folder_name"]
        self.name_edit.blockSignals(True)
        self.name_edit.setText(entry["folder_name"])
        self.name_edit.blockSignals(False)
        self._reset_link()
        playlists = config_manager.list_playlists(self.config)
        pos = next((i for i, e in enumerate(playlists, 1) if e["folder_name"] == entry["folder_name"]), 1)
        self.position_label.setText(f"{pos}.")
        self._show_icon()
        self._update_pills()
        self._clear_selected()
        self.name_edit.clearFocus()
        self.scroll.setFocus()
        try:
            self.folder = str(config_manager.get_playlist_folder(self.config, entry))
        except ValueError:
            self.folder = None
        self._set_rows([], {}, "No Music folder is set yet, so there are no local songs to show.")
        self._update_dirty()
        if self.folder is None:
            return
        self._loading = True
        self._empty_text = "Loading songs…"
        self.empty_label.setText(self._empty_text)
        self.empty_label.show()
        thread = self.ctx.backend.run(ui_backend.edit_data_task(entry, self.folder), heavy=False)
        thread.resultReady.connect(self._on_loaded)
        thread.failed.connect(self._on_load_failed)

    def _on_load_failed(self, message: str) -> None:
        self._loading = False
        self._set_rows([], {}, f"Couldn't read this playlist's songs: {message}")

    def _on_loaded(self, data) -> None:
        if self.entry is None:
            return
        self._loading = False
        rows: List[Row] = []
        have = set()
        for t in data.tracks:
            pm = QPixmap.fromImage(t.thumb) if t.thumb is not None else None
            rows.append(Row(t.key or t.path, t.number, t.title, t.artist, t.album, t.seconds, t.path, pm,
                            own_file=t.own_file, disc=t.disc))
            have.add(t.key)
        cache = ui_settings.get_cache(ui_settings.playlist_key(self.entry))
        for m in cache.get("missing") or []:
            if m.get("key") and m["key"] not in have:
                rows.append(Row(m["key"], int(m.get("number") or 0), m.get("title", ""), m.get("artist", ""),
                                m.get("album", ""), float(m.get("seconds") or 0), None, None,
                                data.failures.get(m["key"]), disc=int(m.get("disc") or 0)))
        # v0.16.39 (Checklist item 46): a spacer where each item Spotify
        # gives no details for sits (from the last Scan / Sync).
        for u in cache.get("spotify_unavailable") or []:
            if u.get("key") and u["key"] in have:  # v0.16.40: its own file was imported
                continue
            number = int(u.get("number") or 0)
            reason = u.get("reason_text") or "Spotify gives no details for it"
            name = u.get("name") or ""
            rows.append(Row(f"{UNAVAILABLE_KEY_PREFIX}{number}", number, name or UNAVAILABLE_TITLE,
                            f"{UNAVAILABLE_TITLE} · {reason}" if name else reason, "", 0.0,
                            unavailable=reason, spotify_id=u.get("key") or ""))
        # v0.16.48 (Checklist item 41a): an album's discs in order, and the
        # disc only shown ("2-1") when the album has more than one.
        if not config_manager.is_album(self.entry) or not any(r.disc > 1 for r in rows):
            for r in rows:
                r.disc = 0
        rows.sort(key=lambda r: (r.disc, r.number if r.number else 10 ** 6, r.title.lower()))
        self._set_rows(rows, data.exceptions, "This playlist has no songs downloaded yet. Run a sync first.")

    def _set_rows(self, rows: List[Row], saved_exceptions: Dict[str, str], empty_text: str) -> None:
        for w in list(self._widgets.values()):
            self.rows_layout.removeWidget(w)
            w.deleteLater()
        self._widgets.clear()
        self._rows = rows
        self._saved_exceptions = dict(saved_exceptions)
        for r in rows:
            w = TrackRow(r)
            w.selected.connect(self._select)
            w.selected.connect(self._leave_field_soon)
            w.playRequested.connect(self._play_key)
            w.playRequested.connect(self._leave_field_soon)
            w.exceptionRequested.connect(self._open_exception_dialog)
            w.importRequested.connect(self._import_for)
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, w)
            self._widgets[r.key] = w
        self._empty_text = empty_text
        self._apply_filter()  # also the empty-list text
        self._refresh_exception_ui()
        if self._playing_key in self._widgets:
            self._widgets[self._playing_key].set_playing(True)

    # ------------------------------------------------------------------
    # Exceptions (pending until Save)
    # ------------------------------------------------------------------
    def _effective(self) -> Dict[str, str]:
        eff = dict(self._saved_exceptions)
        for k, v in self._pending.items():
            if v:
                eff[k] = v
            else:
                eff.pop(k, None)
        return eff

    def _own_files(self) -> Dict[str, str]:
        """key -> Exceptions-list subtitle for every song with its own file
        (v0.16.40): saved ones ("Own file", unless a new exception link is
        pending for it) and pending imports ("Own file · not saved yet")."""
        own = {r.key: "Own file" for r in self._rows
               if r.own_file and not self._pending.get(r.key)}
        for key in self._imports:
            own[key] = "Own file · not saved yet"
        return own

    def _refresh_exception_ui(self) -> None:
        eff = self._effective()
        own = self._own_files()
        for key, w in self._widgets.items():
            w.set_exception(key in eff and key not in own, own_file=key in own)
        # table of contents: Exceptions
        entries = []
        for r in self._rows:
            if r.key in own:
                choice = self._imports.get(r.key)
                entries.append(TocEntry(r, own[r.key], title=choice.title if choice and choice.spacer else ""))
            elif r.key in eff:
                entries.append(TocEntry(r))
        self.exc_section.set_entries(entries, "info")
        self._refresh_issues(eff, own)
        self._update_pills()
        self._refresh_selected()

    def _refresh_issues(self, eff: Dict[str, str], own: Dict[str, str]) -> None:
        """Issues / Pending (v0.16.43, Checklist item 38): every song on
        the page that isn't downloaded, in track order, with its status as
        the song list shows it ("No match", "Age-restricted", "Not
        downloaded", "Unavailable on Spotify"...). A fix waiting for Save
        is noted after it. The count pill is red if any is red, else amber."""
        entries, red = [], False
        for r in self._rows:
            if r.path is not None:
                continue
            if r.unavailable is not None:
                text, role, tip = UNAVAILABLE_TITLE, "faint", _unavailable_details(r)
            else:
                text, role, tip = _failure_display(r)
                red = red or role == "bad"
            if r.key in self._imports:
                text += " · own file not saved yet"
            elif r.key in self._pending and self._pending[r.key]:
                text += " · new link not saved yet"
            elif r.key in eff and r.key not in own:
                text += " · has an exception"
            entries.append(TocEntry(r, text, title=r.title, subtitle_role=role, tooltip=tip))
        self.issue_section.set_entries(entries, "bad" if red else "warn")

    @staticmethod
    def _collapsed_sections() -> dict:
        saved = ui_settings.get("edit_sections_collapsed")
        return saved if isinstance(saved, dict) else {}

    def _section_toggled(self, name: str, collapsed: bool) -> None:
        state = dict(self._collapsed_sections())
        state[name] = collapsed
        ui_settings.set_value("edit_sections_collapsed", state)
        self._animate_sections(self.exc_section if name == "exceptions" else self.issue_section)

    def _fit_sections(self) -> None:
        """An open list takes a share of the column's spare height, a closed
        one only its header; with both closed the spare height sits above
        the bottom-aligned Selected Track box."""
        for section in (self.exc_section, self.issue_section):
            self._right.setStretchFactor(section, 0 if section.collapsed else 1)

    # ---- the lists sliding open / closed (v0.17.0) -----------------------------
    def _animate_sections(self, toggled: "TocSection") -> None:
        """`toggled` was just opened / closed (its state is set; the layout
        hasn't moved yet). Work out where both lists end up, then glide
        both heights there together while its arrow turns. A closing list
        keeps showing its rows until it is closed."""
        sections = (self.exc_section, self.issue_section)
        old = {s: s.height() for s in sections}  # as on screen (mid-slide too)
        self._finish_section_anim()
        self._fit_sections()
        if not animations_on() or not self.isVisible():
            return
        self._right.activate()  # lay out the new state now, to measure it
        new = {s: s.height() for s in sections}
        if all(old[s] == new[s] for s in sections):
            return
        toggled.scroll.setVisible(True)
        for s in sections:
            s.setFixedHeight(old[s])
        self._sec_moves = [(s, old[s], new[s]) for s in sections]
        opening = not toggled.collapsed
        self._sec_turn = (toggled, 0.0 if opening else 180.0, 180.0 if opening else 0.0)
        toggled.chevron.set_rotation(self._sec_turn[1])
        self._sec_anim.setDuration(ANIM_OPEN_MS if opening else ANIM_CLOSE_MS)
        run_value_anim(self._sec_anim, 0.0, 1.0)

    def _sections_step(self, t: float) -> None:
        for s, a, b in self._sec_moves:
            s.setFixedHeight(round(a + (b - a) * t))
        if self._sec_turn is not None:
            s, r0, r1 = self._sec_turn
            s.chevron.set_rotation(r0 + (r1 - r0) * t)

    def _finish_section_anim(self) -> None:
        """End a slide (finished, or interrupted by another toggle): the
        lists go back to the layout's normal sizing."""
        self._sec_anim.stop()
        for s, _a, _b in self._sec_moves:
            s.setMinimumHeight(0)
            s.setMaximumHeight(MAX_HEIGHT)
            s.scroll.setVisible(not s.collapsed)
            s.chevron.set_rotation(0 if s.collapsed else 180)
        self._sec_moves = []
        self._sec_turn = None

    def _update_pills(self) -> None:
        if self.entry is None:
            return
        n = len([k for k in self._effective() if k not in self._imports])  # an import replaces the link
        self.exc_pill.setText(f"{n} Exception{'s' if n != 1 else ''}")
        self.exc_pill.set_tone("info" if n else "neutral")
        cache = ui_settings.get_cache(ui_settings.playlist_key(self.entry))
        unavailable = cache.get("spotify_unavailable") or []
        self.unavail_pill.setText(ui_backend.unavailable_text(len(unavailable)))
        self.unavail_pill.setToolTip(ui_backend.unavailable_tooltip(unavailable) if unavailable else "")
        self.unavail_pill.setVisible(bool(unavailable))
        pending = cache.get("pending")
        red = ui_backend.red_text(int(cache.get("no_match") or 0), int(cache.get("unavailable") or 0))
        self.status_pill.setToolTip(
            ui_backend.red_tooltip(int(cache.get("no_match") or 0), int(cache.get("unavailable") or 0)))
        if cache.get("scanned_at") is None or pending is None:
            self.status_pill.setText("Not checked")
            self.status_pill.set_tone("neutral")
        elif red:
            self.status_pill.setText(f"{pending} pending · {red}" if pending else red)
            self.status_pill.set_tone("bad")
        elif pending == 0:
            self.status_pill.setText("In Sync")
            self.status_pill.set_tone("ok")
        else:
            self.status_pill.setText(f"{pending} pending")
            self.status_pill.set_tone("warn")

    def _row_for(self, key: str) -> Optional[Row]:
        return next((r for r in self._rows if r.key == key), None)

    def _set_pending(self, key: str, video_id: Optional[str]) -> None:
        """Record a change, dropping it again if it equals what's saved."""
        saved = self._saved_exceptions.get(key)
        if video_id == saved or (video_id is None and saved is None):
            self._pending.pop(key, None)
        else:
            self._pending[key] = video_id
        if video_id:
            self._imports.pop(key, None)  # v0.16.40: a new link replaces a pending import
        self._refresh_exception_ui()
        self._update_dirty()

    def _open_exception_dialog(self, key: str) -> None:
        row = self._row_for(key)
        if row is None:
            return
        if row.unavailable is not None:  # a spacer has nothing to link; it can only import (v0.16.40)
            self._import_for(key)
            return
        self._select(key)
        dlg = ui_dialogs.ExceptionLinkDialog(self, row.title, self._effective().get(key))
        if dlg.exec() != dlg.Accepted:
            return
        if dlg.action == "import":
            self._import_for(key)
        elif dlg.action == "remove":
            self._set_pending(key, None)
        elif dlg.action == "save":
            vid = _video_id(dlg.link)
            if vid:
                self._set_pending(key, vid)

    # ---- own files (v0.16.40, Checklist item 47) -------------------------
    def _import_for(self, key: str) -> None:
        """Choose the user's own audio file for this song and confirm it
        (ImportFileDialog). Kept pending until Save, replacing a pending
        exception change for the song."""
        row = self._row_for(key)
        if row is None:
            return
        self._select(key)
        if self.folder is None:
            ui_dialogs.warn(self, "No Music folder", "Set a Music folder first, so the file has a place to go.")
            return
        spacer = row.unavailable is not None
        if spacer and not row.spotify_id:
            ui_dialogs.warn(self, "Can't import here",
                            "Spotify didn't give an ID for this song, so a file can't be tied to its place "
                            "in the playlist.")
            return
        start = os.path.dirname(self._imports[key].source_path) if key in self._imports else self._import_dir
        path = ui_dialogs.choose_audio_file(self, start)
        if not path:
            return
        replaces = []
        if row.path:
            replaces.append("It replaces the song's current file when you press Save.")
        if key in self._effective():
            replaces.append("Its exception link is removed.")
        dlg = ui_dialogs.ImportFileDialog(
            self, path, row.title, row.artist, spacer,
            prefill=metadata_tagger.read_source_tags(path) if spacer else None,
            replaces=" ".join(replaces),
        )
        if dlg.exec() != dlg.Accepted:
            return
        self._import_dir = os.path.dirname(dlg.path)
        self._imports[key] = ui_backend.ImportChoice(
            source_path=dlg.path, spacer=spacer, spotify_id=row.spotify_id, position=row.number,
            title=dlg.value("title"), artist=dlg.value("artist"), album=dlg.value("album"), year=dlg.value("year"),
        )
        if self._pending.get(key):  # a pending new link would be replaced by the file anyway
            self._pending.pop(key, None)
        self._refresh_exception_ui()
        self._update_dirty()

    def _cancel_import(self, key: str) -> None:
        if self._imports.pop(key, None) is not None:
            self._refresh_exception_ui()
            self._update_dirty()

    def _url_edited(self) -> None:
        key = self._selected
        if not key or key not in self._effective():
            return
        text = self.url_edit.text().strip()
        current = self._effective().get(key)
        if not text:
            self.url_hint.setText("")
            self.url_edit.setText(_watch_url(current))
            return
        vid = _video_id(text)
        if not vid:
            self.url_edit.setProperty("invalid", True)
            _repolish(self.url_edit)
            self._link_checker.check(None)
            self._set_url_hint("That doesn't look like a YouTube link or video ID.", "bad")
            return
        self.url_edit.setProperty("invalid", False)
        _repolish(self.url_edit)
        if vid != current:
            self._set_pending(key, vid)  # refreshes the box, which checks the new link
        else:
            self._check_link(vid)

    def _set_url_hint(self, text: str, role: str) -> None:
        self.url_hint.setText(text)
        self.url_hint.setProperty("role", role)
        _repolish(self.url_hint)
        self.url_hint.setVisible(bool(text))

    def _check_link(self, vid: Optional[str]) -> None:
        """Check the selected song's exception video (v0.16.38)."""
        self._set_url_hint("", "muted")
        if self._link_checker.check(vid):
            self._set_url_hint("Checking the video on YouTube…", "muted")

    def _on_link_checked(self, vid: str, text: str, role: str) -> None:
        if self._selected and self._effective().get(self._selected) == vid:
            self._set_url_hint(text, role)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def _select(self, key: str) -> None:
        if self._selected in self._widgets:
            self._widgets[self._selected].set_selected(False)
        self._selected = key
        if key in self._widgets:
            self._widgets[key].set_selected(True)
        self._refresh_selected()

    def _select_and_scroll(self, key: str) -> None:
        self._select(key)
        w = self._widgets.get(key)
        if w:
            ensure_visible(self.scroll, w, 0, 60)  # v0.17.0: glides
            # Rows just shown or hidden by the search are only laid out on
            # the next event-loop turn, so scroll again once they are.
            QTimer.singleShot(0, lambda: self._scroll_to(key))

    def _scroll_to(self, key: str) -> None:
        w = self._widgets.get(key)
        if w is not None and ui_keys.is_valid(w) and not w.isHidden():
            ensure_visible(self.scroll, w, 0, 60)

    def _centre_on(self, key: str) -> None:
        """Scroll so this song sits in the middle of the list (v0.16.32)."""
        w = self._widgets.get(key)
        if w is not None and ui_keys.is_valid(w) and not w.isHidden():
            scroll_area_to(self.scroll, w.y() + w.height() // 2 - self.scroll.viewport().height() // 2)

    def _leave_field_soon(self, _key: str = "") -> None:
        """A song was clicked (v0.16.32): if a field on this page still has
        the typing cursor (usually the search bar), leave it, so W/S, Q,
        E... act on the clicked song instead of typing. Clicking a song
        doesn't take the focus from the field by itself. Deferred so it
        runs after the click is handled; leaving keeps the typed text."""
        if ui_keys.is_typing_widget(QApplication.focusWidget()):
            QTimer.singleShot(0, self._leave_field)

    def _leave_field(self) -> None:
        fw = QApplication.focusWidget()
        if ui_keys.is_valid(self) and ui_keys.is_typing_widget(fw) and self.isAncestorOf(fw):
            self.setFocus(Qt.OtherFocusReason)

    def _toc_clicked(self, key: str) -> None:
        """An Exceptions list entry: select and show its song. If the
        search hides it (v0.16.31), the search is cleared first."""
        w = self._widgets.get(key)
        if w is not None and w.isHidden():
            self._select(key)  # first, so clearing scrolls to this song
            self.search.clear()
        self._select_and_scroll(key)

    # ------------------------------------------------------------------
    # Search bar (v0.16.31, Checklist item 32)
    # ------------------------------------------------------------------
    def _song_matches(self, row: Row) -> bool:
        """Every word typed appears in the title, artist or album, in any
        order, ignoring case. A plain number also matches the track number
        ("12" finds track 12 and "1912"); "#12" matches track 12 only."""
        text = f"{row.title} {row.artist} {row.album}".lower()
        for word in self.search.text().lower().split():
            if word.startswith("#") and word[1:].isdigit():
                if int(word[1:]) != row.number:
                    return False
            elif word not in text and not (word.isdigit() and int(word) == row.number):
                return False
        return True

    def _shown_keys(self) -> List[str]:
        return [r.key for r in self._rows if r.key in self._widgets and not self._widgets[r.key].isHidden()]

    def _apply_filter(self) -> None:
        """Show only the matching songs. Called as the search text changes
        and after every list rebuild. Nothing else changes: the selected
        song stays selected (and in the Selected Track box) even when
        hidden, and a hidden song keeps playing."""
        shown = 0
        for r in self._rows:
            w = self._widgets.get(r.key)
            if w is None:
                continue
            show = self._song_matches(r)
            w.setVisible(show)
            shown += show
        query = self.search.text().strip()
        if self._rows:
            self.empty_label.setText(f"No songs match “{query}”.")
        else:
            self.empty_label.setText(self._empty_text)
        self.empty_label.setVisible(not shown)
        self.search_count.setText(
            f"{shown} of {len(self._rows)} song{'s' if len(self._rows) != 1 else ''}" if query and self._rows else "")
        # v0.16.32: clearing the search scrolls the full list to the
        # selected song (once the rows just shown are laid out).
        if self._last_query and not query and self._selected in self._widgets:
            QTimer.singleShot(0, lambda k=self._selected: self._centre_on(k))
        self._last_query = query

    def _search_entered(self) -> None:
        """Enter in the search bar keeps the search and leaves the bar (the
        keyboard controller does that) and selects the first song shown,
        so Q, E, W/S... carry on from there."""
        shown = self._shown_keys()
        if shown:
            self._select_and_scroll(shown[0])

    def _kbd_search(self) -> None:
        """Ctrl+F or /: type in the search bar, its text selected."""
        if ui_keys.usable(self.search):
            self._kbd_field(self.search)

    def _clear_selected(self) -> None:
        self.sel_title.setText("Select a song")
        self.sel_artist.setText("")
        self.sel_num.setText("")
        self.sel_thumb.set_pixmap(None)
        self.sel_thumb.set_placeholder_name("?")
        self.sel_failure.hide()
        self.url_box.hide()
        self.import_box.hide()

    def _show_sel_note(self, text: str, role: str) -> None:
        self.sel_failure.setText(text)
        self.sel_failure.setProperty("role", role)
        _repolish(self.sel_failure)
        self.sel_failure.show()

    def _refresh_selected(self) -> None:
        row = self._row_for(self._selected) if self._selected else None
        if row is None:
            self._clear_selected()
            return
        self.sel_title.setText(row.title)
        self.sel_artist.setText(row.artist)
        self.sel_num.setText(str(row.number) if row.number else "")
        self.sel_thumb.set_placeholder_name(row.album or row.title)
        self.sel_thumb.set_pixmap(row.pixmap)
        choice = self._imports.get(row.key)
        if choice is not None:  # v0.16.40
            named = f" as “{choice.title}” by {choice.artist}" if choice.spacer else ""
            self._show_sel_note(f"Own file, not saved yet: {os.path.basename(choice.source_path)}{named}.\n"
                                f"It is imported when you press Save. The original file stays where it is.", "link")
        elif row.unavailable is not None:
            self._show_sel_note(_unavailable_details(row) + "\nUse your own file to fill this place.", "muted")
        elif row.own_file and not self._pending.get(row.key):
            self._show_sel_note(f"Own file: {row.own_file}.\nImported from your own audio file, not "
                                f"downloaded. Syncs keep it; an exception link would replace it.", "muted")
        elif row.path is None and row.failure:
            _text, role, details = _failure_display(row)
            self._show_sel_note(details, role)
        else:
            self.sel_failure.hide()
        self.cancel_import_btn.setVisible(choice is not None)
        self.import_btn.setText("Choose another file…" if choice is not None else "Use your own file…")
        self.import_box.show()
        vid = self._effective().get(row.key)
        if vid:
            if not self.url_edit.hasFocus():
                self.url_edit.setText(_watch_url(vid))
                self.url_edit.setCursorPosition(0)
            self.url_edit.setProperty("invalid", False)
            _repolish(self.url_edit)
            if vid != self._checked_vid or not self.url_box.isVisible():
                self._checked_vid = vid
                self._check_link(vid)
            self.url_box.show()
        else:
            self._checked_vid = None
            self._link_checker.check(None)
            self.url_box.hide()

    # ------------------------------------------------------------------
    # Name / icon / logs
    # ------------------------------------------------------------------
    def _show_icon(self) -> None:
        self.icon.set_placeholder_name(self.name_edit.text() or self._orig_name)
        if self._pending_icon is not None:
            self.icon.set_pixmap(self._pending_icon)
            return
        pm = None
        if self.entry:
            path = ui_settings.get_icon(ui_settings.playlist_key(self.entry))
            if path:
                pm = QPixmap(path)
        self.icon.set_pixmap(pm)

    def _on_name_changed(self, _text: str) -> None:
        if self._pending_icon is None and not (self.entry and ui_settings.get_icon(ui_settings.playlist_key(self.entry))):
            self.icon.set_placeholder_name(self.name_edit.text() or self._orig_name)
        self._update_dirty()

    # ---- playlist link (Checklist item 16) -------------------------------
    def _reset_link(self) -> None:
        self.link_edit.blockSignals(True)
        self.link_edit.setText(playlist_link(self.entry) if self.entry else "")
        self.link_edit.setCursorPosition(0)
        self.link_edit.blockSignals(False)
        self._show_link_hint()

    def _link_state(self):
        """('same' | 'changed' | 'invalid', (platform, source id) or None)
        for what's typed in the link field, compared with the saved link.
        A different spelling of the same playlist (e.g. with ?si=…) is
        'same'."""
        if self.entry is None:
            return "same", None
        # v0.16.48 (Checklist item 41a): (platform, kind, id), so an album
        # link counts as a change of kind.
        detected = config_manager.detect_source(self.link_edit.text().strip())
        if detected is None:
            return "invalid", None
        current = config_manager.entry_source(self.entry)
        if detected[0] == config_manager.PLATFORM_SOUNDCLOUD:
            # v0.16.52: a SoundCloud set link doesn't say whether it's an album;
            # another set keeps this entry's kind (as config_manager does on Save).
            kind = current[1] if current[0] == config_manager.PLATFORM_SOUNDCLOUD else config_manager.KIND_PLAYLIST
            detected = (detected[0], kind, detected[2])
        return ("same" if config_manager.same_source(detected, current) else "changed"), detected

    def _link_conflict(self, detected) -> Optional[str]:
        """Name of another tracked playlist that already uses this link."""
        for other in config_manager.list_playlists(self.config):
            if other is self.entry:
                continue
            if config_manager.same_source(config_manager.entry_source(other), detected):  # v0.16.52
                return other["folder_name"]
        return None

    def _show_link_hint(self) -> None:
        state, detected = self._link_state()
        text, role = "", ""
        if state == "invalid":
            text = config_manager.unrecognised_source_message(self.link_edit.text().strip()) \
                if (config_manager.extract_artist_id(self.link_edit.text().strip())
                    or config_manager.is_soundcloud_short_link(self.link_edit.text().strip())
                    or "music.apple.com" in self.link_edit.text() or "music.amazon." in self.link_edit.text()) \
                else ("That isn't a Spotify, YT Music, Apple Music, Amazon Music or SoundCloud playlist or album, or a YouTube "
                      "playlist link.")
            role = "bad"
        elif state == "changed":
            other = self._link_conflict(detected)
            if other:
                what = "album" if detected[1] == config_manager.KIND_ALBUM else "playlist"
                text, role = f"That {what} is already tracked as “{other}”.", "bad"
            else:
                new_label = _platform_label(detected[0], detected[1])
                current = config_manager.entry_source(self.entry)
                if detected[:2] != current[:2]:
                    article = "an" if new_label[0] in "AEIOU" else "a"
                    target = f"{article} {new_label}" if detected[1] == config_manager.KIND_ALBUM else new_label
                    text = f"Switches this to {target}. Applied on Save, after a warning."
                elif detected[1] == config_manager.KIND_ALBUM:
                    text = f"A different {new_label}. Applied on Save, after a warning."
                else:
                    text = f"A different {new_label} playlist. Applied on Save, after a warning."
                role = "warn"
        self.link_edit.setProperty("invalid", role == "bad")
        _repolish(self.link_edit)
        self.link_hint.setText(text)
        self.link_hint.setProperty("role", role)
        _repolish(self.link_hint)
        self.link_hint.setVisible(bool(text))

    def _on_link_changed(self, _text: str) -> None:
        self._show_link_hint()
        self._update_dirty()

    def _link_edited(self) -> None:
        # Tidy a re-pasted copy of the same playlist back to its usual form.
        if self.entry is not None and self._link_state()[0] == "same" and self.link_edit.text().strip() != playlist_link(self.entry):
            self._reset_link()

    def _change_icon(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a picture for this playlist", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if not path:
            return
        pm = QPixmap(path)
        if pm.isNull():
            ui_dialogs.warn(self, "Couldn't open that image", "That file couldn't be read as a picture.")
            return
        self._pending_icon = pm
        self.icon.set_pixmap(pm)
        self._update_dirty()

    def _view_logs(self) -> None:
        if not self.folder:
            ui_dialogs.info(self, "Sync Logs", "No Music folder is set yet.")
            return
        path = os.path.join(self.folder, library_sync.DOWNLOAD_LOG_FILENAME)
        if not os.path.exists(path):
            ui_dialogs.info(self, "Sync Logs", "This playlist has no Sync Log yet — one is written after a sync that downloads songs.")
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            ui_dialogs.warn(self, "Sync Logs", f"Couldn't open the log: {e}")
            return
        ui_dialogs.TextViewerDialog(self, f"Sync Log — {self._orig_name}", text, subtitle=path).exec()

    # ------------------------------------------------------------------
    # Dirty state, Save, Back
    # ------------------------------------------------------------------
    def has_unsaved(self) -> bool:
        if self.entry is None:
            return False
        return bool(self._pending) or bool(self._imports) or self._pending_icon is not None or \
            self.name_edit.text().strip() != self._orig_name or \
            self._link_state()[0] != "same"

    def _update_dirty(self) -> None:
        d = self.has_unsaved()
        self.save_btn.setEnabled(d)
        self.dirtyChanged.emit(d)

    def request_back(self) -> None:
        if self.confirm_leave():
            self.stop_player()
            self.backRequested.emit()

    def confirm_leave(self) -> bool:
        """True if it's fine to leave this page (nothing unsaved, or the
        user chose Save / Discard)."""
        if not self.has_unsaved():
            return True
        dlg = ui_dialogs.UnsavedChangesDialog(self)
        dlg.exec()
        if dlg.choice == "save":
            return self.save()
        if dlg.choice == "discard":
            self._discard_changes()
            return True
        return False

    def _discard_changes(self) -> None:
        self._pending = {}
        self._imports = {}
        self._pending_icon = None
        self.name_edit.blockSignals(True)
        self.name_edit.setText(self._orig_name)
        self.name_edit.blockSignals(False)
        self._reset_link()
        self._update_dirty()

    def _confirm_link_change(self) -> Optional[bool]:
        """Checks and confirms a pending link change before anything is
        saved. None = no link change; True = confirmed; False = stop the
        whole Save (nothing has been applied yet)."""
        state, detected = self._link_state()
        if state == "same":
            return None
        if state == "invalid":
            ui_dialogs.warn(
                self, "Playlist link not recognised",
                "The link isn't a Spotify, YT Music, Apple Music, Amazon Music or SoundCloud playlist or album, or a YouTube "
                "playlist link. "
                "Fix it, or press "
                "Back and choose Discard to keep the current link.",
            )
            return False
        other = self._link_conflict(detected)
        if other:
            ui_dialogs.warn(self, "Playlist already tracked",
                            f"That playlist is already tracked as “{other}”, so it can't be used here too.")
            return False
        if self._pending or self._imports:
            ui_dialogs.warn(
                self, "Save the exception changes first",
                "Exception changes and own files belong to the current playlist's songs, so they can't be "
                "saved together with a new playlist link. Put the link back and press Save, "
                "then change the link.",
            )
            return False
        old_platform, old_kind, _old_id = config_manager.entry_source(self.entry)
        dlg = ui_dialogs.ConfirmLinkChangeDialog(
            self, self._orig_name, playlist_link(self.entry), source_link(detected[0], detected[2], detected[1]),
            _platform_label(old_platform, old_kind), _platform_label(detected[0], detected[1]),
        )
        return dlg.exec() == dlg.Accepted

    def save(self) -> bool:
        """Apply the pending link / name / icon / exception changes. Blocks
        (with a progress dialog) until finished; returns True on success."""
        if self.entry is None or not self.has_unsaved():
            return True
        if self.ctx.backend.heavy_running:
            ui_dialogs.info(self, "Please wait", "Another task is still running. Try again in a moment.")
            return False

        link_change = self._confirm_link_change()
        if link_change is False:
            return False

        entry = self.entry
        new_name = self.name_edit.text().strip()
        old_name = entry["folder_name"]
        key = ui_settings.playlist_key(entry)

        # Let go of the audio file first: Windows can't rename a folder or
        # replace a file while it is open.
        self.stop_player()

        if link_change:
            try:
                entry = config_manager.update_playlist_source_id(
                    self.config, old_name, self.link_edit.text().strip(), allow_platform_change=True
                )
            except ValueError as e:
                ui_dialogs.warn(self, "Couldn't change the playlist link", str(e))
                return False
            new_key = ui_settings.playlist_key(entry)
            ui_settings.playlist_source_changed(key, new_key)
            key = new_key
            self.entry = entry
            self._reset_link()

        if new_name and new_name != old_name:
            try:
                entry = config_manager.rename_playlist(self.config, old_name, new_name)
            except ValueError as e:
                ui_dialogs.warn(self, "Couldn't rename the playlist", str(e))
                if link_change:  # the new link was already saved
                    self.ctx.on_config_changed()
                return False
            self.entry = entry
            self._orig_name = entry["folder_name"]
            try:
                self.folder = str(config_manager.get_playlist_folder(self.config, entry))
            except ValueError:
                pass
            self.name_edit.blockSignals(True)
            self.name_edit.setText(self._orig_name)
            self.name_edit.blockSignals(False)

        if self._pending_icon is not None:
            os.makedirs(ui_settings.ICONS_DIR, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
            dest = os.path.join(ui_settings.ICONS_DIR, f"{safe}.png")
            if self._pending_icon.save(dest, "PNG"):
                ui_settings.set_icon(key, dest)
            else:
                ui_dialogs.warn(self, "Couldn't save the picture", "The playlist image couldn't be written to disk.")
            self._pending_icon = None

        failures = []
        if self._pending or self._imports:
            changes = dict(self._pending)
            imports = dict(self._imports)  # v0.16.40 (Checklist item 47)
            busy = ui_dialogs.BusyDialog(self, "Saving changes",
                                         "Applying exceptions and own files…" if imports else "Applying exceptions…")
            holder = {}

            thread = self.ctx.backend.run(ui_backend.save_exceptions_task(entry, self.folder, changes, imports))
            thread.progress.connect(busy.set_message)
            relay = _Relay(busy, holder)
            thread.resultReady.connect(relay.on_result)
            thread.failed.connect(relay.on_failed)
            busy.exec()
            if "error" in holder:
                failures.append(holder["error"])
            for k, ok, msg in holder.get("outcomes", []):
                if not ok:
                    r = self._row_for(k)
                    choice = imports.get(k)
                    title = choice.title if choice and choice.spacer else (r.title if r else k)
                    failures.append(f"{title}: {msg}")
            self._pending = {}
            self._imports = {}

        if failures:
            ui_dialogs.warn(self, "Some changes weren't applied", "\n\n".join(failures))

        self.ctx.on_config_changed()
        self.open_entry(self.entry, keep_search=True)
        return not failures

    # ------------------------------------------------------------------
    # Player
    # ------------------------------------------------------------------
    def _playable_keys(self) -> List[str]:
        return [r.key for r in self._rows if r.path]

    def _set_player_empty(self) -> None:
        self._playing_key = None
        self.pl_title.setText("Nothing playing")
        self.pl_artist.setText("Click a song’s picture to play it")
        self.pl_thumb.set_pixmap(None)
        self.pl_thumb.set_placeholder_name("♪")
        self.play_btn.set_kind("play")
        for b in (self.play_btn, self.prev_btn, self.next_btn, self.slider):
            b.setEnabled(False)
        self.slider.setValue(0)
        self.time_now.setText("0:00")
        self.time_total.setText("0:00")
        for w in self._widgets.values():
            w.set_playing(False)

    def stop_player(self) -> None:
        if self.player is not None:
            try:
                self.player.stop()
                self.player.setSource(QUrl())
            except Exception:
                pass
        self._set_player_empty()

    def _play_key(self, key: str) -> None:
        row = self._row_for(key)
        if self.player is None:
            ui_dialogs.info(self, "Player unavailable", "Audio playback isn't available on this install.")
            return
        if row is None or not row.path:
            return
        self._select(key)
        if self._playing_key in self._widgets:
            self._widgets[self._playing_key].set_playing(False)
        self._playing_key = key
        if key in self._widgets:
            self._widgets[key].set_playing(True)
        self.pl_title.setText(row.title)
        self.pl_artist.setText(row.artist)
        self.pl_thumb.set_placeholder_name(row.album or row.title)
        self.pl_thumb.set_pixmap(row.pixmap)
        self.time_total.setText(format_duration(row.seconds))
        for b in (self.play_btn, self.prev_btn, self.next_btn, self.slider):
            b.setEnabled(True)
        self.player.setSource(QUrl.fromLocalFile(row.path))
        self.player.play()

    def _toggle_play(self) -> None:
        if self.player is None or self._playing_key is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _step(self, delta: int) -> None:
        keys = self._playable_keys()
        if not keys or self._playing_key not in keys:
            return
        i = keys.index(self._playing_key) + delta
        if 0 <= i < len(keys):
            self._play_key(keys[i])
        elif delta > 0:
            self.stop_player()

    def _prev(self) -> None:
        if self.player is not None and self.player.position() > 3000:
            self.player.setPosition(0)
        else:
            self._step(-1)

    def _next(self) -> None:
        self._step(1)

    def _on_state(self, state) -> None:
        if self._playing_key is None:
            return
        self.play_btn.set_kind("pause" if state == QMediaPlayer.PlayingState else "play")

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.EndOfMedia:
            self._step(1)

    def _on_player_error(self, *_args) -> None:
        if self.player is not None and self._playing_key:
            logger.log_error(f"GUI: player error: {self.player.errorString()}")
            self.pl_artist.setText("Couldn't play this file")

    def _on_volume(self, value: int) -> None:
        # Linear, like the fixed 0.85 it replaces (85 = the same loudness).
        if self.audio is not None:
            self.audio.setVolume(value / 100)
        self.vol_icon.set_level(value / 100)
        self.vol_slider.setToolTip(f"Volume: {value}%")

    def _on_duration(self, ms: int) -> None:
        if ms > 0:
            self.time_total.setText(format_duration(ms / 1000))

    def _on_position(self, ms: int) -> None:
        if self._seeking or self.player is None:
            return
        dur = self.player.duration()
        self.slider.setValue(int(ms * 1000 / dur) if dur else 0)
        self.time_now.setText(format_duration(ms / 1000))

    def _seek_begin(self) -> None:
        self._seeking = True

    def _seek_end(self) -> None:
        self._seeking = False
        if self.player is not None and self.player.duration():
            self.player.setPosition(int(self.slider.value() / 1000 * self.player.duration()))

    def hideEvent(self, e):
        # Leaving the page (Settings etc.) pauses playback; it starts
        # empty again every time Edit is opened.
        if self.player is not None and self._playing_key and \
                self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        super().hideEvent(e)

    # ------------------------------------------------------------------
    # Keyboard control (v0.16.23, Checklist item 27 part 2)
    # ------------------------------------------------------------------
    # ui_keys.KeyboardController calls these while this page is showing
    # with no overlay or dialog open and no text field being typed in.
    # Each key calls the same handler as its button or click, so the
    # mouse's rules (disabled buttons, nothing loaded) apply as they are.
    KEY_SCREEN = "edit"

    def eventFilter(self, obj, e):
        if e.type() == QEvent.FocusIn and obj in (self.name_edit, self.link_edit, self.url_edit, self.search):
            self._field_before[obj] = obj.text()
        return super().eventFilter(obj, e)

    def key_field_escape(self, field: QWidget) -> bool:
        """Esc while typing in the name, link, Exception URL or search
        field: put back what was there when the cursor entered it and
        leave the field. The next Esc clears the search, if any, then is
        Back."""
        if field not in (self.name_edit, self.link_edit, self.url_edit, self.search):
            return False
        before = self._field_before.get(field)
        if before is not None and field.text() != before:
            field.setText(before)
            field.setCursorPosition(0)
        self.setFocus(Qt.OtherFocusReason)
        return True

    def _kbd_step_song(self, delta: int) -> None:
        """W/S: select the song above / below (the first one if none is
        selected yet), scrolling it into view. Songs the search hides are
        skipped; from a selected song the search hides, W/S go to the first
        song shown (v0.16.31)."""
        keys = self._shown_keys()
        if not keys:
            return
        if self._selected in keys:
            i = keys.index(self._selected) + delta
            if not 0 <= i < len(keys):
                return
        else:
            i = 0
        self._select_and_scroll(keys[i])

    def _kbd_play_selected(self) -> None:
        if self._selected and self._row_for(self._selected):
            self._play_key(self._selected)

    def _kbd_play_pause(self) -> None:
        """Space: play / pause what is loaded; with nothing loaded, play
        the selected song (like Q)."""
        if self._playing_key is None:
            self._kbd_play_selected()
        else:
            self._toggle_play()

    def _kbd_seek(self, delta_ms: int) -> None:
        if self.player is None or self._playing_key is None:
            return
        pos = max(0, self.player.position() + delta_ms)
        dur = self.player.duration()
        if dur:
            pos = min(pos, dur)
        self.player.setPosition(pos)

    def _kbd_volume(self, delta: int) -> None:
        if self.vol_slider.isEnabled():
            self.vol_slider.setValue(self.vol_slider.value() + delta)

    def _kbd_field(self, field: QLineEdit) -> None:
        """2 / 3: type in the name / link field, its text selected."""
        field.setFocus(Qt.ShortcutFocusReason)
        field.selectAll()

    def _kbd_cancel(self) -> None:
        """X: the only task that can run while this page shows is the
        launch-time scan; cancel it as "Cancel Scan, Use Cache" does."""
        win = getattr(self.ctx, "window", None)
        if win is not None:
            win.main_page.cancel_launch_scan()

    def key_pressed(self, a: set, _e) -> bool:
        if "up" in a or "down" in a:
            self._kbd_step_song(-1 if "up" in a else 1)
        elif "seek_back" in a or "seek_forward" in a:
            self._kbd_seek(-SEEK_STEP_MS if "seek_back" in a else SEEK_STEP_MS)
        elif "volume_down" in a or "volume_up" in a:
            self._kbd_volume(-VOLUME_STEP if "volume_down" in a else VOLUME_STEP)
        elif "play_selected" in a:
            self._kbd_play_selected()
        elif "play_pause" in a:
            self._kbd_play_pause()
        elif "prev_track" in a:
            ui_keys.press(self.prev_btn)
        elif "next_track" in a:
            ui_keys.press(self.next_btn)
        elif "exception" in a:
            if self._selected and self._row_for(self._selected):
                self._open_exception_dialog(self._selected)
        elif "import_file" in a:  # v0.16.40 (Checklist item 47)
            if self._selected and self._row_for(self._selected):
                self._import_for(self._selected)
        elif "icon" in a:
            self._change_icon()
        elif "name" in a:
            self._kbd_field(self.name_edit)
        elif "link" in a:
            self._kbd_field(self.link_edit)
        elif "logs" in a:
            ui_keys.press(self.logs_btn)
        elif "save" in a:
            ui_keys.press(self.save_btn)
        elif "search" in a:
            self._kbd_search()
        elif "back" in a:
            if self.search.text():  # v0.16.31: clear the search first
                self.search.clear()
            else:
                ui_keys.press(self.back_btn)
        elif "cancel" in a:
            self._kbd_cancel()
        else:
            header = getattr(getattr(self.ctx, "window", None), "header", None)
            btn = None
            if header is not None:
                btn = header.gear_btn if "settings" in a else header.cmd_btn if "cmd" in a else None
            if btn is None:
                return False
            ui_keys.press(btn)
        return True

    def key_released(self, _actions: set, _e) -> None:
        pass

    def key_reset(self) -> None:
        pass


class _Relay(QObject):
    """GUI-thread receiver that closes the Saving dialog when the worker
    finishes (a bound method of a GUI-thread QObject => queued call)."""

    def __init__(self, busy, holder):
        super().__init__(busy)
        self._busy = busy
        self._holder = holder

    def on_result(self, outcomes):
        self._holder["outcomes"] = outcomes
        self._busy.finish()

    def on_failed(self, message):
        self._holder["error"] = message
        self._busy.finish()
