"""
Mud Music Manager - Edit page rows (split out of ui_edit_page.py in v0.23.4)

A song as the Edit page shows it (Row), its line in the song list
(TrackRow), the right column's collapsible lists (TocSection, TocEntry)
and the status / number helpers they share with the page. What they do
is described in ui_edit_page.py's docstring and Design Doc §3.10.
"""

from dataclasses import dataclass
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

import config_manager
import exceptions_manager
import track_failures
from ui_widgets import (
    ElidedLabel, GlyphButton, PillLabel, ThumbLabel, format_duration, make_button, smooth_scroll,
)



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
