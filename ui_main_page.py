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
"""

import os
import time
from dataclasses import replace
from typing import Dict, List, Optional

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QBrush, QColor, QDesktopServices, QGradient, QIntValidator, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QSizePolicy,
    QSpacerItem, QStackedWidget, QVBoxLayout, QWidget,
)

import config_manager
import library_sync
import track_failures
import ui_backend
import ui_dialogs
import ui_keys
import ui_settings
import artist_watch
import logger
import ytmusic_reader
from ui_widgets import (
    AlbumBadge, CapsLabel, ClickableElidedLabel, Drawer, DrawerSlide, ElidedLabel, GlyphButton,
    PillLabel, PlatformBadge, RoundCheck, SegmentedProgressBar, Spinner, ThumbLabel, ensure_visible,
    animations_on, format_duration, hseparator, make_button, make_panel, make_value_anim, mix_color, motion_curve,
    run_value_anim, scroll_area_to, smooth_scroll, smooth_scrolling_on, ANIM_OPEN_MS,
)

ANIM_REORDER_MS = 220  # v0.17.0: a reordered bar sliding to its new place
from ui_theme import palette


def _repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def _park_focus(holder: QWidget, page: Optional[QWidget]) -> None:
    """Call before hiding or disabling `holder` (a button, or a panel of
    them). If it holds the keyboard focus, Qt would otherwise pass focus on
    to the next widget in the Tab order -- the first playlist's position
    box -- and put a typing cursor in it (v0.16.15). Focus goes to the page
    instead, so Esc still reaches it and nothing is selected for editing."""
    fw = QApplication.focusWidget()
    if page is not None and fw is not None and (fw is holder or holder.isAncestorOf(fw)):
        page.setFocus(Qt.OtherFocusReason)


def paint_open_bar(bar: QFrame, t: float, pick_hover: bool = False) -> None:
    """The open look of a playlist / artist bar (teal gradient, brighter
    border), painted over the closed look the stylesheet draws, at strength
    t (0 closed .. 1 open), so it blends in as the bar opens (v0.17.0;
    it used to switch on at once through a stylesheet property). In Remove
    mode the hovered bar keeps the stylesheet's red border."""
    if t <= 0.0:
        return
    pal = palette()
    p = QPainter(bar)
    p.setRenderHint(QPainter.Antialiasing)
    r = QRectF(bar.rect())
    fill = QPainterPath()
    fill.addRoundedRect(r.adjusted(1, 1, -1, -1), 13, 13)
    g = QLinearGradient(0, 0, 1, 1)
    g.setCoordinateMode(QGradient.ObjectBoundingMode)
    g.setColorAt(0, QColor(pal.row_exp1))
    g.setColorAt(1, QColor(pal.row_exp2))
    p.setOpacity(min(1.0, t))
    p.fillPath(fill, QBrush(g))
    p.setOpacity(1.0)
    if not pick_hover:
        p.setPen(QPen(mix_color(QColor(pal.row_border), QColor(pal.row_exp_border), t), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 13.5, 13.5)


def relative_time(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    delta = max(0, time.time() - ts)
    if delta < 45:
        return "just now"
    if delta < 3600:
        m = int(delta // 60) or 1
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if delta < 86400:
        h = int(delta // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(delta // 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


def source_link(platform: str, sid: str, kind: str = config_manager.KIND_PLAYLIST) -> str:
    # v0.17.6: the links are built in config_manager, so the Sync Log can use them too
    return config_manager.source_link(platform, sid, kind)


def playlist_link(entry: dict) -> str:
    return config_manager.entry_link(entry)


def short_link(url: str) -> str:
    return url.replace("https://", "").replace("http://", "").replace("www.", "")


def load_icon_pixmap(entry: dict) -> Optional[QPixmap]:
    path = ui_settings.get_icon(ui_settings.playlist_key(entry))
    if path:
        pm = QPixmap(path)
        if not pm.isNull():
            return pm
    return None


# ---------------------------------------------------------------------------
# One playlist in the list
# ---------------------------------------------------------------------------

class PlaylistRow(QFrame):
    editRequested = Signal(str)          # folder_name
    moveRequested = Signal(str, int)     # folder_name, delta (-1 / +1)
    positionChosen = Signal(str, int)    # folder_name, new 1-based position
    removePicked = Signal(str)           # folder_name
    expandedChanged = Signal(str, bool)  # playlist key, expanded
    revealWanted = Signal(object)        # v0.17.0: opened by the user; scroll it into view
    checkedChanged = Signal()
    rowClicked = Signal(str)             # playlist key (v0.16.28: the keyboard highlight follows)
    expandClicked = Signal(str, str)     # playlist key, "click" / "double" / "right" (v0.16.35)

    def __init__(self, entry: dict, position: int, total: int, parent=None):
        super().__init__(parent)
        self.setObjectName("row")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.entry = entry
        self.key = ui_settings.playlist_key(entry)
        self.position = position
        self.total = total
        self.first, self.last = 1, total  # its section's positions (v0.16.49, set_position())
        self._expanded = False
        self._select_mode = False
        self._pick_mode = False
        self._hover = False
        self._double = False  # the left click being released is a double click's second
        self.setMouseTracking(True)

        # v0.16.34 (Checklist item 35): slimmer bars. Smaller top / bottom
        # margins, shorter ▲ / ▼ buttons and no padding around the position
        # number; the text, arrows, thumbnail, badge and pills keep their size.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 6, 16, 6)
        outer.setSpacing(0)

        # ---- top line -------------------------------------------------
        top = QHBoxLayout()
        top.setSpacing(12)
        outer.addLayout(top)

        self.check = RoundCheck()
        self.check.setChecked(False)
        self.check.toggled.connect(lambda _c: self.checkedChanged.emit())
        self.check.hide()
        top.addWidget(self.check)

        reorder = QVBoxLayout()
        reorder.setSpacing(0)
        reorder.setContentsMargins(0, 0, 0, 0)
        self.up_btn = GlyphButton("up", 22, height=14)
        self.up_btn.setToolTip("Move up")
        self.up_btn.clicked.connect(lambda: self.moveRequested.emit(self.entry["folder_name"], -1))
        self.number = QLineEdit(str(position))
        self.number.setProperty("flat", True)
        self.number.setProperty("compact", True)
        self.number.setAlignment(Qt.AlignCenter)
        self.number.setFixedWidth(46)
        self.number.setValidator(QIntValidator(1, max(total, 1)))
        f = self.number.font()
        f.setPointSizeF(17)
        f.setBold(True)
        self.number.setFont(f)
        self.number.setToolTip("Click and type a new position")
        self.number.editingFinished.connect(self._number_edited)
        self.down_btn = GlyphButton("down", 22, height=14)
        self.down_btn.setToolTip("Move down")
        self.down_btn.clicked.connect(lambda: self.moveRequested.emit(self.entry["folder_name"], +1))
        reorder.addWidget(self.up_btn, 0, Qt.AlignHCenter)
        reorder.addWidget(self.number, 0, Qt.AlignHCenter)
        reorder.addWidget(self.down_btn, 0, Qt.AlignHCenter)
        top.addLayout(reorder)

        self.thumb = ThumbLabel(46, 0.22)
        top.addWidget(self.thumb)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title = ElidedLabel("")
        tf = self.title.font()
        tf.setPointSizeF(12.5)
        tf.setBold(True)
        self.title.setFont(tf)
        title_row.addWidget(self.title, 0)
        self.badge = PlatformBadge(config_manager.get_playlist_platform(entry), 26)
        title_row.addWidget(self.badge, 0, Qt.AlignVCenter)
        # v0.16.48 (Checklist item 41a): the album icon, on albums only.
        # v0.17.0: created inside the bar. Made visible while it had no
        # parent yet, it flashed up as a tiny window of its own on every
        # list rebuild (each ▲ / ▼ / Shift+W / Shift+S), taking the focus.
        self.album_badge = AlbumBadge(26, parent=self)
        self.album_badge.setVisible(config_manager.is_album(entry))
        title_row.addWidget(self.album_badge, 0, Qt.AlignVCenter)
        title_row.addStretch(1)
        text_col.addLayout(title_row)
        # Elided (not a plain QLabel) so a narrow window shrinks the text
        # instead of pushing the status pill / chevron / Edit out of view.
        self.details = ElidedLabel("")
        self.details.setProperty("role", "muted")
        text_col.addWidget(self.details)
        top.addLayout(text_col, 1)

        # The "N unavailable on Spotify" pill is shown on the Edit page
        # only (v0.16.39 had it here too; removed in v0.16.41).
        # v0.16.27 (Checklist item 28): red pill for tracks shown as
        # "No match" / "Unavailable", beside the usual status pill.
        self.alert = PillLabel("", "bad", height=28, font_px=14)
        self.alert.hide()
        top.addWidget(self.alert, 0, Qt.AlignVCenter)
        self.status = PillLabel("Not checked", "neutral", height=28, font_px=14)
        top.addWidget(self.status, 0, Qt.AlignVCenter)
        self.chevron = GlyphButton("chev_down", 30)
        self.chevron.clicked.connect(lambda: self.rowClicked.emit(self.key))
        self.chevron.clicked.connect(self.toggle_expanded)
        self.chevron._opacity = 0.0
        top.addWidget(self.chevron, 0, Qt.AlignVCenter)

        # ---- expanded details ----------------------------------------
        self.extra = QWidget()
        extra_lay = QVBoxLayout(self.extra)
        extra_lay.setContentsMargins(0, 12, 0, 2)
        extra_lay.setSpacing(10)
        extra_lay.addWidget(hseparator())
        # 12 px fixed gap + a 22 px gap that gives way when the window is
        # narrow (34 px apart normally, never less than 12).
        cols = QHBoxLayout()
        cols.setSpacing(12)
        self.col_tracks = self._column("Tracks", cols)
        self.col_length = self._column("Length", cols)
        self.col_exceptions = self._column("Exceptions", cols)
        link_box = QVBoxLayout()
        link_box.setSpacing(3)
        self.link_caption = CapsLabel("Playlist link", muted=True)
        link_box.addWidget(self.link_caption)
        self.link_label = ClickableElidedLabel("")
        self.link_label.setProperty("role", "link")
        self.link_label.setCursor(Qt.PointingHandCursor)
        self.link_label.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(playlist_link(self.entry))))
        link_box.addWidget(self.link_label)
        cols.addLayout(link_box, 1)
        cols.addSpacerItem(self._shrinking_gap())
        self.edit_btn = make_button("Edit")
        self.edit_btn.clicked.connect(lambda: self.editRequested.emit(self.entry["folder_name"]))
        cols.addWidget(self.edit_btn, 0, Qt.AlignBottom)
        extra_lay.addLayout(cols)
        # v0.17.0: the details slide down from under the top line at their
        # full size (a Drawer), while the arrow turns and the open bar's
        # colours blend in, all on one motion (DrawerSlide).
        self.drawer = Drawer(self.extra, "bottom")
        outer.addWidget(self.drawer)
        self._slide = DrawerSlide(self.drawer, on_value=self._opened_by)

        self.refresh_text()

    def _opened_by(self, t: float) -> None:
        self.chevron.set_rotation(180 * t)
        self.update()  # the open bar's colours (paintEvent)

    def paintEvent(self, e):
        super().paintEvent(e)
        paint_open_bar(self, self._slide.t, self._pick_mode and self.underMouse())

    def _column(self, title: str, layout: QHBoxLayout) -> QLabel:
        box = QVBoxLayout()
        box.setSpacing(3)
        box.addWidget(CapsLabel(title, muted=True))
        value = QLabel("—")
        vf = value.font()
        vf.setBold(True)
        value.setFont(vf)
        box.addWidget(value)
        layout.addLayout(box)
        layout.addSpacerItem(self._shrinking_gap())
        return value

    @staticmethod
    def _shrinking_gap() -> QSpacerItem:
        return QSpacerItem(22, 0, QSizePolicy.Preferred, QSizePolicy.Minimum)

    # ---- content -----------------------------------------------------
    def refresh_text(self, stats: Optional[dict] = None) -> None:
        name = self.entry["folder_name"]
        self.title.setText(name)
        self.title.setMaximumWidth(max(120, self.title.fontMetrics().horizontalAdvance(name) + 12))
        self.thumb.set_placeholder_name(name)
        self.thumb.set_pixmap(load_icon_pixmap(self.entry))
        self.badge.set_platform(config_manager.get_playlist_platform(self.entry))
        album = config_manager.is_album(self.entry)
        self.album_badge.setVisible(album)
        if album:
            self.badge.setToolTip(config_manager.source_label(*config_manager.entry_source(self.entry)[:2]))
        self.link_caption.setText("ALBUM LINK" if album else "PLAYLIST LINK")
        stats = stats if stats is not None else ui_settings.get_cache(self.key)
        tracks = stats.get("tracks")
        seconds = stats.get("seconds")
        exceptions = stats.get("exceptions")
        platform_name = config_manager.source_label(*config_manager.entry_source(self.entry)[:2])
        if tracks is None:
            self.details.setText(f"— Tracks · {platform_name}")
            for lab in (self.col_tracks, self.col_length, self.col_exceptions):
                lab.setText("—")
        else:
            self.details.setText(
                f"{tracks} Track{'s' if tracks != 1 else ''} · {format_duration(seconds or 0, True)} · "
                f"{exceptions or 0} Exception{'s' if (exceptions or 0) != 1 else ''} · {platform_name}"
            )
            self.col_tracks.setText(str(tracks))
            self.col_length.setText(format_duration(seconds or 0, True))
            self.col_exceptions.setText(f"{exceptions or 0} track{'s' if (exceptions or 0) != 1 else ''}")
        link = playlist_link(self.entry)
        self.link_label.setText(short_link(link))
        self.link_label.setToolTip(link)

    def set_position(self, position: int, total: int, first: int = 1, last: Optional[int] = None) -> None:
        """first / last (v0.16.49): the positions of this row's section
        (playlists or albums); it can only be moved between them."""
        self.position, self.total = position, total
        self.first, self.last = first, (total if last is None else last)
        self.number.setText(str(position))
        self.number.setValidator(QIntValidator(1, max(total, 1)))
        self.up_btn.setEnabled(position > self.first)
        self.down_btn.setEnabled(position < self.last)

    def set_indent(self, px: int) -> None:
        """Move the bar in from the left (v0.16.50: an artist's albums sit
        under its bar)."""
        self.layout().setContentsMargins(14 + px, 6, 16, 6)

    def set_status(self, text: str, tone: str, tooltip: str = "", alert: str = "",
                   alert_tooltip: str = "") -> None:
        """alert: the red pill's text ("" hides it). With an alert and
        nothing pending, pass text="" to show only the red pill."""
        self.status.setText(text)
        self.status.set_tone(tone)
        self.status.setToolTip(tooltip)
        self.status.setVisible(bool(text))
        self.alert.setText(alert)
        self.alert.setToolTip(alert_tooltip)
        self.alert.setVisible(bool(alert))

    def _number_edited(self) -> None:
        text = self.number.text().strip()
        if text.isdigit():
            n = max(self.first, min(int(text), self.last))  # v0.16.49: within its section
            if n != self.position:
                self.positionChosen.emit(self.entry["folder_name"], n)
                return
        self.number.setText(str(self.position))

    # ---- expand / collapse ------------------------------------------
    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._update_chevron()
        if expanded:
            self._slide.open(animate)
        else:
            self._slide.close(animate)
        self.expandedChanged.emit(self.key, expanded)

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)
        if self._expanded:
            self.revealWanted.emit(self)  # v0.17.0: the page scrolls it into view

    # ---- modes -------------------------------------------------------
    def set_select_mode(self, on: bool, checked: bool = False) -> None:
        self._select_mode = on
        self.check.setVisible(on)
        if on:
            self.check.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor if (on or self._pick_mode) else Qt.ArrowCursor)

    def set_pick_mode(self, on: bool) -> None:
        self._pick_mode = on
        self.setProperty("pick", on)
        _repolish(self)
        self.setCursor(Qt.PointingHandCursor if (on or self._select_mode) else Qt.ArrowCursor)

    def is_checked(self) -> bool:
        return self.check.isChecked()

    def set_controls_enabled(self, enabled: bool) -> None:
        for w in (self.up_btn, self.down_btn, self.number, self.edit_btn):
            w.setEnabled(enabled and (w not in (self.up_btn, self.down_btn)
                                      or (w is self.up_btn and self.position > self.first)
                                      or (w is self.down_btn and self.position < self.last)))

    # ---- hover + clicks ---------------------------------------------
    def _update_chevron(self) -> None:
        self.chevron.fade_to(1.0 if (self._hover or self._expanded) else 0.0)  # v0.17.0: fades

    def enterEvent(self, e):
        self._hover = True
        self._update_chevron()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._update_chevron()
        super().leaveEvent(e)

    # v0.16.35 (Checklist item 36): a left click only selects the playlist
    # (the page puts the highlight on it). The page opens / closes it on a
    # double click, a right click, or a left click on the playlist already
    # selected. Ticking (choosing) and picking (Remove) are unchanged.
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._double = False
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        super().mouseDoubleClickEvent(e)  # QWidget's calls mousePressEvent, so this goes after
        if e.button() == Qt.LeftButton:
            self._double = True

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.rowClicked.emit(self.key)  # first, so Remove's popup opens with the highlight here
            if self._pick_mode:
                self.removePicked.emit(self.entry["folder_name"])
            elif self._select_mode:
                self.check.toggle()
            else:
                self.expandClicked.emit(self.key, "double" if self._double else "click")
        elif e.button() == Qt.RightButton:
            self.rowClicked.emit(self.key)
            self.expandClicked.emit(self.key, "right")
        super().mouseReleaseEvent(e)


# ---------------------------------------------------------------------------
# An artist in the Albums list (v0.16.50, Checklist items 41b / 41c)
# ---------------------------------------------------------------------------

def artist_key(artist_id: str) -> str:
    """ui_settings key of an artist (its picture): 'spotify:artist:<id>',
    or 'ytmusic:artist:<id>' for a YT Music artist (v0.16.51)."""
    return f"{config_manager.id_platform(artist_id)}:artist:{artist_id}"


class ArtistRow(QFrame):
    """An artist's bar, above its albums (Michael's choice): round picture,
    name, how many releases are tracked, whether it is watched for new
    releases. Expanded: Watching on / off, Add releases… (the discography
    picker again) and the artist's link. While choosing playlists its tick
    ticks / unticks all its albums; in Remove mode a click picks it."""

    watchToggled = Signal(str, bool)   # artist id, watch
    addRequested = Signal(str)         # artist id
    removePicked = Signal(str)         # artist id
    checkToggled = Signal(str, bool)   # artist id, ticked (its albums follow)
    expandedChanged = Signal(str, bool)
    revealWanted = Signal(object)      # v0.17.0

    def __init__(self, artist: dict, album_count: int, parent=None):
        super().__init__(parent)
        self.setObjectName("row")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.artist = artist
        self.artist_id = config_manager.artist_id_of(artist)  # Spotify or YT Music (v0.16.51)
        self.platform = config_manager.artist_platform(artist)
        self.key = artist_key(self.artist_id)
        self._expanded = False
        self._select_mode = False
        self._pick_mode = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 6, 16, 6)
        outer.setSpacing(0)
        top = QHBoxLayout()
        top.setSpacing(12)
        outer.addLayout(top)

        self.check = RoundCheck()
        self.check.clicked.connect(lambda c: self.checkToggled.emit(self.artist_id, c))
        self.check.hide()
        top.addWidget(self.check)
        top.addSpacing(46 - 12)  # lines the picture up with the playlists' pictures
        self.thumb = ThumbLabel(46, 0.5)
        self.thumb.set_placeholder_name(artist.get("name") or "?")
        path = ui_settings.get_icon(self.key)
        if path:
            self.thumb.set_pixmap(QPixmap(path))
        top.addWidget(self.thumb)

        col = QVBoxLayout()
        col.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title = ElidedLabel(artist.get("name") or "?")
        tf = self.title.font()
        tf.setPointSizeF(12.5)
        tf.setBold(True)
        self.title.setFont(tf)
        title_row.addWidget(self.title, 0)
        badge = PlatformBadge(self.platform, 26)
        badge.setToolTip(f"{config_manager.platform_name(self.platform)} artist")
        title_row.addWidget(badge, 0, Qt.AlignVCenter)
        title_row.addStretch(1)
        col.addLayout(title_row)
        self.details = ElidedLabel("")
        self.details.setProperty("role", "muted")
        col.addWidget(self.details)
        top.addLayout(col, 1)

        self.status = PillLabel("", "neutral", height=28, font_px=14)
        top.addWidget(self.status, 0, Qt.AlignVCenter)
        self.chevron = GlyphButton("chev_down", 30)
        self.chevron.clicked.connect(self.toggle_expanded)
        top.addWidget(self.chevron, 0, Qt.AlignVCenter)

        self.extra = QWidget()
        ex = QVBoxLayout(self.extra)
        ex.setContentsMargins(0, 12, 0, 2)
        ex.setSpacing(10)
        ex.addWidget(hseparator())
        cols = QHBoxLayout()
        cols.setSpacing(16)
        self.watch_btn = make_button("")
        self.watch_btn.clicked.connect(lambda: self.watchToggled.emit(self.artist_id, not self.artist.get("watch")))
        cols.addWidget(self.watch_btn, 0, Qt.AlignBottom)
        self.add_btn = make_button("Add releases…")
        self.add_btn.setToolTip("Choose more of this artist's albums and singles to add")
        self.add_btn.clicked.connect(lambda: self.addRequested.emit(self.artist_id))
        cols.addWidget(self.add_btn, 0, Qt.AlignBottom)
        link_box = QVBoxLayout()
        link_box.setSpacing(3)
        link_box.addWidget(CapsLabel("Artist link", muted=True))
        link = artist_watch.artist_link(self.artist_id)
        self.link_label = ClickableElidedLabel(short_link(link))
        self.link_label.setProperty("role", "link")
        self.link_label.setCursor(Qt.PointingHandCursor)
        self.link_label.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(link)))
        link_box.addWidget(self.link_label)
        cols.addLayout(link_box, 1)
        ex.addLayout(cols)
        # v0.17.0: opens like a playlist bar (see PlaylistRow)
        self.drawer = Drawer(self.extra, "bottom")
        outer.addWidget(self.drawer)
        self._slide = DrawerSlide(self.drawer, on_value=self._opened_by)

        self.refresh(album_count)

    def _opened_by(self, t: float) -> None:
        self.chevron.set_rotation(180 * t)
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        paint_open_bar(self, self._slide.t, self._pick_mode and self.underMouse())

    def refresh(self, album_count: int) -> None:
        watch = bool(self.artist.get("watch"))
        checked = relative_time(self.artist.get("last_checked"))
        self.details.setText(
            f"{album_count} release{'s' if album_count != 1 else ''} · "
            f"{config_manager.platform_name(self.platform)} artist · "
            + (f"watching for new releases (last checked {checked})" if watch else "not watching"))
        self.status.setText("Watching" if watch else "Not watching")
        self.status.set_tone("ok" if watch else "neutral")
        self.status.setToolTip("New releases are added and downloaded automatically with every Scan / Sync"
                               if watch else "New releases aren't added; turn Watching on to add them")
        self.watch_btn.setText("Stop watching" if watch else "Watch for new releases")

    # ---- expand / modes ------------------------------------------------
    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        if expanded == self._expanded:
            return
        self._expanded = expanded
        if expanded:
            self._slide.open(animate)
        else:
            self._slide.close(animate)
        self.expandedChanged.emit(self.key, expanded)

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)
        if self._expanded:
            self.revealWanted.emit(self)  # v0.17.0: the page scrolls it into view

    def set_select_mode(self, on: bool, checked: bool = False) -> None:
        self._select_mode = on
        self.check.setVisible(on)
        if on:
            self.check.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor if (on or self._pick_mode) else Qt.ArrowCursor)

    def set_pick_mode(self, on: bool) -> None:
        self._pick_mode = on
        self.setProperty("pick", on)
        _repolish(self)
        self.setCursor(Qt.PointingHandCursor if (on or self._select_mode) else Qt.ArrowCursor)

    def set_controls_enabled(self, enabled: bool) -> None:
        for w in (self.watch_btn, self.add_btn):
            w.setEnabled(enabled)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            if self._pick_mode:
                self.removePicked.emit(self.artist_id)
            elif self._select_mode:
                self.check.setChecked(not self.check.isChecked())
                self.checkToggled.emit(self.artist_id, self.check.isChecked())
            else:
                self.toggle_expanded()
        super().mouseReleaseEvent(e)


# ---------------------------------------------------------------------------
# Add queue (v0.16.33, Checklist item 34)
# ---------------------------------------------------------------------------

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

class MainPage(QWidget):
    openEdit = Signal(dict)
    openResults = Signal(str, list)   # kind ('scan' | 'integrity'), [PlaylistRunResult]
    refreshRequested = Signal()       # ask the app shell to re-scan in the background

    MODE_TITLES = {"sync": "Sync Now", "scan": "Scan", "integrity": "Check Integrity"}

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
        for b in (self.sync_btn, self.scan_btn, self.integrity_btn):
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
        return super().eventFilter(obj, e)

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
                  "integrity": ("Check Integrity", "Tick the playlists whose files should be checked.", "Check Files")}
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
        else:
            self._start_integrity(entries)

    # ------------------------------------------------------------------
    # Running things
    # ------------------------------------------------------------------
    def set_busy(self, busy: bool) -> None:
        for b in (self.sync_btn, self.scan_btn, self.integrity_btn, self.add_btn, self.remove_btn, self.change_btn):
            if busy:
                _park_focus(b, self)
            b.setEnabled(not busy)
        if busy:  # the launch-time scan is being stopped for this task anyway
            _park_focus(self.use_cache_btn, self)
            self.use_cache_btn.hide()
        for row in list(self._rows.values()) + list(self._artist_rows.values()):
            row.set_controls_enabled(not busy)
        self._update_add_btn()

    def _update_add_btn(self) -> None:
        """While playlists are being added (v0.16.33, Checklist item 34) the
        Add button stays usable as "Queue Another Playlist"; after Cancel
        all, it waits for the run to stop."""
        queueing = self._adds is not None
        self.add_btn.setText("+  Queue Another Playlist" if queueing else "+  Add")
        self.add_btn.setToolTip("Add another playlist once the ones ahead of it are done" if queueing else "")
        if queueing:
            usable = not self._adds_cancel_all
            if not usable:
                _park_focus(self.add_btn, self)
            self.add_btn.setEnabled(usable)

    def _need_library(self, entries: List[dict]) -> bool:
        """Entries without a custom_path live under the Music folder, so
        one has to be set (the terminal menu says the same)."""
        if config_manager.get_library_path(self.config):
            return True
        if all(e.get("custom_path") for e in entries):
            return True
        if ui_dialogs.ask_yes_no(
            self, "No Music folder yet",
            "No Music folder is set yet. Choose one first?", "Choose folder", "Cancel",
        ):
            self._on_change_folder()
        return False

    def _start_sync(self, entries: List[dict]) -> None:
        if not entries or self.backend.heavy_running or not self._need_library(entries):
            return
        self._running_kind = "sync"
        self.panel.show_running("Sync Progress:", len(entries), True, True, "Preparing…")
        self._syncs = [QueuedSync(e) for e in entries]
        self.panel.bar.set_weights(bar_weights(self._syncs))  # v0.17.6: sized by track count
        self.panel.start_clock()
        self.set_busy(True)
        self._refresh_sync_ui()
        self.backend.when_refresh_stopped(lambda: self._launch_sync(entries))

    def _launch_sync(self, entries: List[dict]) -> None:
        self._control = ui_backend.SyncControl(self)
        self._control.stateChanged.connect(self.panel.set_control_state)
        thread = self.backend.run(ui_backend.sync_task(self.config, entries, self._control,
                                                       self.apply_new_releases_call))  # v0.16.50
        thread.progressInfo.connect(self._on_sync_progress)
        thread.entryStarted.connect(self._on_sync_entry_started)
        thread.entryDone.connect(self._on_sync_entry_done)
        thread.localStats.connect(self.on_local_stats)
        thread.resultReady.connect(self._on_sync_finished)
        thread.cancelled.connect(self._on_sync_cancelled)
        thread.failed.connect(self._on_task_failed)

    def _on_sync_entry_started(self, entry: dict) -> None:
        self.on_entry_started(entry, syncing=True)
        item = self._sync_item(entry)
        if item is not None:
            item.state, item.text = "syncing", "Preparing…"
            self._refresh_sync_ui()

    # ---- Sync Now's queue list (v0.16.36, Checklist item 37) ----------
    def _sync_item(self, entry: dict) -> Optional[QueuedSync]:
        key = ui_settings.playlist_key(entry)
        return next((i for i in self._syncs or [] if i.key == key), None)

    def _refresh_sync_ui(self) -> None:
        """Sync Now's playlists changed state: rebuild the queue list, and
        Cancel reads Cancel all while some are waiting."""
        if self._syncs is None:
            return
        self.panel.set_queue(self._syncs, live=True)
        # more than one left (the first may not have started yet)
        several = sum(1 for i in self._syncs if i.state in ("waiting", "syncing")) > 1
        if self._control is None or not self._control.cancel_requested:
            self.panel.cancel_btn.setText("Cancel all" if several else "Cancel")
        self.panel.cancel_btn.setToolTip(
            "Stop the playlist being synced and skip the ones waiting" if several else "")

    def _on_sync_progress(self, p) -> None:
        """The usual progress line, and the same news on the queue line of
        the playlist being synced. v0.17.6: once it has been read, its
        segment of the bar takes its real track count."""
        item = next((i for i in self._syncs or [] if i.state == "syncing"), None)
        if item is not None and p.track_total and item.size != p.track_total:
            item.size = p.track_total
            self.panel.bar.set_weights(bar_weights(self._syncs))
        self.panel.update_progress(p)
        if item is None or item.stopping or (self._control is not None and self._control.cancel_requested):
            return
        parts = [p.text] if p.text else []
        if p.track_total:
            parts.append(f"Track {p.track_index}/{p.track_total}")
        item.text = "  ·  ".join(parts)
        self.panel.update_queue_item(item)

    def _on_sync_entry_done(self, run) -> None:
        self.on_entry_done(run)
        item = self._sync_item(run.entry)
        if item is None:
            return
        item.run, item.stopping = run, False
        if run.stopped:
            item.state = "stopped"
            item.text = (f"Stopped · {run.stopped_downloaded} downloaded before stopping. "
                         "Sync Now finishes it.")
        elif run.error:
            item.state, item.text = "sync_failed", f"Sync failed: {run.error}"
        else:
            item.state, item.text = "synced", self._run_result_line(run)
        self._refresh_sync_ui()

    def _on_sync_item_stop(self, item: QueuedSync) -> None:
        """✕ on the playlist being synced: stop that playlist only, at the
        next safe point (after the current download); the rest carry on."""
        if self._syncs is None or item not in self._syncs or item.state != "syncing" or self._control is None:
            return
        _park_focus(self.panel, self)
        if self._control.request_stop_playlist(item.key):
            item.stopping = True
            item.text = "Stopping this playlist — finishing the current download…"
            self._refresh_sync_ui()

    def _end_sync_queue(self, cancelled: bool = False, failed: str = "") -> None:
        """Sync Now ended: the list stays readable (no ✕) until Dismiss."""
        items, self._syncs = self._syncs, None
        if not items:
            return
        for item in items:
            if item.state == "syncing":
                if failed:
                    item.state, item.text = "sync_failed", f"Something went wrong: {failed}"
                else:
                    item.state, item.text = "stopped", "Stopped by Cancel. Sync Now finishes it."
            elif item.state == "waiting":
                item.state, item.text = "not_synced", "Not synced (Cancel)" if cancelled else "Not synced"
        self.panel.set_queue(items, live=False)

    def _on_pause_clicked(self) -> None:
        if not self._control:
            return
        _park_focus(self.panel, self)  # the button is disabled while pausing
        if self._control.state == "paused":
            self._control.request_resume()
        else:
            self._control.request_pause()

    def _on_cancel_clicked(self) -> None:
        if self._scan_control is not None:
            self._cancel_scan_use_cache()
        elif self._adds is not None:
            self._cancel_all_adds()
        elif self._control:
            _park_focus(self.panel, self)  # the button is disabled while cancelling
            self._control.request_cancel()
            for item in self._syncs or []:  # v0.16.36: no ✕ while it all stops
                if item.state == "syncing":
                    item.stopping = True
                    item.text = "Stopping — finishing the current download…"
            self._refresh_sync_ui()

    def _sync_report(self, results: list, cancelled: bool) -> tuple:
        """Totals and the Sync Details text. The failed tracks are grouped
        by what they need (v0.16.27, Checklist item 28): No match (an
        exception link), Unavailable (failed on 5+ syncs), or failed this
        time (usually temporary). attention is the short summary for the
        panel, e.g. " · 1 no match · 2 will retry"."""
        downloaded = renumbered = removed = unchanged = 0
        lines = []
        failed_total = 0
        errors = 0
        counts = {track_failures.STATUS_NO_MATCH: 0, track_failures.STATUS_UNAVAILABLE: 0,
                  track_failures.STATUS_RETRY: 0, "other": 0}
        stopped = 0
        for run in results:
            if run.stopped:  # v0.16.36: ✕ stopped this playlist's sync
                stopped += 1
                downloaded += run.stopped_downloaded
                lines.append(f"=== {run.name} ===\nStopped before it finished ({run.stopped_downloaded} "
                             "downloaded first; nothing removed or renumbered). Sync Now finishes it.\n")
                continue
            if run.error:
                errors += 1
                lines.append(f"=== {run.name} ===\nSync failed: {run.error}\n")
                continue
            r = run.result
            ok_dl = sum(1 for d in r.downloaded if d.file_path is not None)
            downloaded += ok_dl
            renumbered += len(r.renumbered)
            removed += len(r.removed)
            unchanged += r.unchanged_count
            failed_total += len(r.failed)
            lines.append(
                f"=== {run.name} ===\n{ok_dl} downloaded, {len(r.renumbered)} renumbered, "
                f"{len(r.removed)} removed, {r.unchanged_count} unchanged, {len(r.failed)} failed/unmatched"
            )
            groups = {track_failures.STATUS_NO_MATCH: [], track_failures.STATUS_UNAVAILABLE: [],
                      track_failures.STATUS_RETRY: []}
            for d in r.failure_details:
                groups[track_failures.status(d)].append(f"  - {d.get('name', '')}: {track_failures.summary(d)}")
            other = [f"  - {n}: {reason}" for n, reason in r.failed if reason == library_sync.TRACK_NUMBER_FAILURE]
            for key, items in groups.items():
                counts[key] += len(items)
            counts["other"] += len(other)
            if groups[track_failures.STATUS_NO_MATCH]:
                lines.append("No match: the search worked, but nothing found was the right length. "
                             "Add an exception link on this playlist's Edit page:")
                lines.extend(groups[track_failures.STATUS_NO_MATCH])
            if groups[track_failures.STATUS_UNAVAILABLE]:
                lines.append(f"Unavailable: age-restricted, or failed on {track_failures.UNAVAILABLE_AFTER} or "
                             "more syncs (still tried on every sync; an exception link may help):")
                lines.extend(groups[track_failures.STATUS_UNAVAILABLE])
            if groups[track_failures.STATUS_RETRY]:
                lines.append("Failed this time, usually temporary (the next sync tries again):")
                lines.extend(groups[track_failures.STATUS_RETRY])
            if other:
                lines.append("Other problems:")
                lines.extend(other)
            lines.append("")
        nothing = (not results) or (
            errors == 0 and failed_total == 0 and stopped == 0 and downloaded == renumbered == removed == 0
        )
        attention = ""
        for key, word in ((track_failures.STATUS_NO_MATCH, "no match"),
                          (track_failures.STATUS_UNAVAILABLE, "unavailable"),
                          (track_failures.STATUS_RETRY, "will retry"),
                          ("other", "other problem" + ("s" if counts["other"] != 1 else ""))):
            if counts[key]:
                attention += f" · {counts[key]} {word}"
        return downloaded, renumbered, removed, failed_total, errors, nothing, "\n".join(lines), attention

    def _on_sync_finished(self, results: list) -> None:
        downloaded, renumbered, removed, failed, errors, nothing, details, attention = self._sync_report(results, False)
        stopped = sum(1 for r in results if r.stopped)
        self._control = None
        self.set_busy(False)
        self.panel.set_bar(self.panel.bar._segments)
        self._last_sync_report = details
        if nothing:
            self.panel.show_finished("Everything is already in sync — nothing to download.", "",
                                     auto_hide_ms=9000, banner="Sync Complete")
        elif stopped and stopped == len(results):
            # v0.16.36: every playlist was stopped with its ✕
            self.panel.show_finished(f"Sync stopped — {downloaded} downloaded before stopping.", details)
        else:
            # "Sync Complete" is shown large in the bar, so the line above
            # carries only the numbers.
            text = f"{downloaded} downloaded, {renumbered} renumbered, {removed} removed"
            if failed:
                text += attention
            if errors:
                text += f" · {errors} playlist(s) couldn't be synced"
            if stopped:
                text += f" · {stopped} stopped"
            self.panel.show_finished(text, details, auto_hide_ms=0 if (failed or errors or stopped) else 15000,
                                     banner="Sync Complete")
        self._end_sync_queue()
        self.refresh_status_card()
        self.reload_stats_only()
        if stopped:
            self.refreshRequested.emit()  # a stopped sync leaves the cached numbers stale

    def _on_sync_cancelled(self, partial: list) -> None:
        downloaded, renumbered, removed, failed, errors, nothing, details, _attention = self._sync_report(partial, True)
        self._control = None
        self.set_busy(False)
        self._last_sync_report = details
        text = f"Sync cancelled — {downloaded} downloaded before stopping."
        self.panel.show_finished(text, details)
        self._end_sync_queue(cancelled=True)
        for key, st in list(self._status.items()):
            if st.get("state") in ("syncing", "checking"):
                self._status[key] = {"state": "unknown"}
        for row in self._rows.values():
            self._apply_cached_status(row)
        self.refresh_status_card()
        self.reload_stats_only()
        self.refreshRequested.emit()  # a cancelled sync leaves the cached numbers stale

    def _on_task_failed(self, message: str) -> None:
        self._control = None
        self._scan_control = None
        self._discard_scan_updates = False
        self.set_busy(False)
        self.panel.show_finished(f"Something went wrong: {message}", message)
        self._end_sync_queue(failed=message)
        for key, st in list(self._status.items()):
            if st.get("state") in ("syncing", "checking"):
                self._status[key] = {"state": "unknown"}
        for row in self._rows.values():
            self._apply_cached_status(row)
        self.refresh_status_card()

    # ------------------------------------------------------------------
    # Add runs and the Add queue (v0.16.33, Checklist item 34)
    # ------------------------------------------------------------------
    # An Add run syncs the playlist just added, then each queued one in
    # turn: config_manager.add_playlist() when its turn comes, then its own
    # sync_task (the same sync as Sync Now), with its own pause / cancel.
    # A queued playlist that can't be added, or whose sync fails, is marked
    # so in the queue list and the run carries on with the next one.

    def _queue_problem(self, data: dict) -> Optional[str]:
        """Why this playlist can't be queued (the checks add_playlist()
        makes, plus the playlists already waiting), or None. Checked now so
        a typo is caught while the dialog is still at hand."""
        source = config_manager.detect_source(data["source"])  # v0.16.48: albums too
        if source is None:
            return config_manager.unrecognised_source_message(data["source"])
        name = data["name"].strip()
        if not name:
            return "Folder name cannot be empty."
        existing = config_manager.find_playlist(self.config, name)
        if existing:
            return f"A tracked playlist already uses the name '{existing['folder_name']}'."
        for entry in config_manager.list_playlists(self.config):
            if config_manager.same_source(config_manager.entry_source(entry), source):  # v0.16.52
                return f"That {'album' if config_manager.is_album(entry) else 'playlist'} " \
                       f"is already tracked as '{entry['folder_name']}'."
        for item in self._adds or []:
            if item.state != "waiting":
                continue  # the ones already added are covered above
            if item.name.strip().lower() == name.lower():
                return f"A queued playlist already uses the name '{item.name}'."
            if config_manager.same_source(config_manager.detect_source(item.data["source"]), source):
                return f"That playlist is already queued as '{item.name}'."
        if data.get("custom_path"):
            if not os.path.isdir(data["custom_path"]):
                return f"That folder doesn't exist: {data['custom_path']}"
        elif not config_manager.get_library_path(self.config):
            return ("No Music folder is set yet, so there's nowhere to download to. Once this add "
                    "finishes, choose one with Change, then add this playlist.")
        return None

    def _refresh_queue_ui(self) -> None:
        """The queue changed (a playlist queued, taken out, started or
        done): rebuild the list and size the bar to the playlists counted."""
        if self._adds is None:
            return
        self.panel.set_queue(self._adds, live=True)
        counted = self._counted_adds()
        self.panel.bar.set_weights(bar_weights(counted))  # v0.17.6: sized by track count
        waiting = any(i.state == "waiting" for i in self._adds)
        if self._control is None or not self._control.cancel_requested:
            self.panel.cancel_btn.setText("Cancel all" if waiting else "Cancel")
        self.panel.cancel_btn.setToolTip(
            "Stop the playlist being added and drop the ones waiting" if waiting else "")

    def _counted_adds(self) -> List[QueuedAdd]:
        """The playlists the bar has a segment for (not those dropped by Cancel all)."""
        return [i for i in self._adds or [] if i.state != "cancelled"]

    def _start_add_run(self, entry: dict, data: dict) -> None:
        """The playlist was just added to playlists.json: sync it, with
        "Queue Another Playlist" usable until the run ends."""
        if self.backend.heavy_running or not self._need_library([entry]):
            return
        item = QueuedAdd(data)
        item.entry, item.state, item.text = entry, "adding", "Preparing…"
        self._adds, self._add_current, self._adds_cancel_all = [item], item, False
        self._running_kind = "sync"
        self.panel.show_running("Sync Progress:", 1, True, True, "Preparing…")
        self.panel.start_clock()  # v0.17.6: one clock for the whole Add run
        self.set_busy(True)
        self._refresh_queue_ui()
        self.backend.when_refresh_stopped(lambda: self._launch_add_item(item))

    def _launch_add_item(self, item: QueuedAdd) -> None:
        if self._adds is None or item is not self._add_current:
            return
        if self._adds_cancel_all:  # Cancel all before its sync could start
            item.state, item.text = "stopped", "Added, not synced yet (cancelled). Sync Now syncs it."
            self._finish_add_run()
            return
        # A pause asked for during the previous playlist holds the queue:
        # this one pauses before it starts reading (asked before its thread
        # starts, so it can't get past that first safe point).
        pause = self._control is not None and self._control.pause_requested
        self.panel.reset_controls()
        self._control = ui_backend.SyncControl(self)
        self._control.stateChanged.connect(self.panel.set_control_state)
        if pause:
            self._control.request_pause()
        thread = self.backend.run(ui_backend.sync_task(self.config, [item.entry], self._control))
        thread.progressInfo.connect(self._on_add_progress)
        thread.entryStarted.connect(self._on_sync_entry_started)
        thread.entryDone.connect(self.on_entry_done)
        thread.localStats.connect(self.on_local_stats)
        thread.resultReady.connect(self._on_add_item_finished)
        thread.cancelled.connect(self._on_add_item_cancelled)
        thread.failed.connect(self._on_add_item_task_failed)
        self._refresh_queue_ui()

    def _on_add_progress(self, p) -> None:
        """The usual progress line, counted over the whole Add run
        ("Playlist 2/3"), and the same news on the playlist's queue line.
        v0.17.6: the segments are sized by track count (this playlist's, once
        read); the ETA covers this playlist, with "+" while others wait."""
        item = self._add_current
        if self._adds is None or item is None:
            return
        counted = self._counted_adds()
        index = counted.index(item) if item in counted else 0
        if p.track_total:
            item.size = p.track_total
        self.panel.bar.set_weights(bar_weights(counted))
        waiting = any(i.state == "waiting" for i in counted)
        self.panel.update_progress(replace(p, playlist_index=index + 1, playlist_total=len(counted),
                                           eta_open=p.eta_open or waiting))
        parts = [p.text] if p.text else []
        if p.track_total:
            parts.append(f"Track {p.track_index}/{p.track_total}")
        if not (self._control is not None and self._control.cancel_requested):
            item.text = "  ·  ".join(parts)
            self.panel.update_queue_item(item)

    def _run_result_line(self, run) -> str:
        """One playlist's result for its queue line (Add run or Sync Now)."""
        downloaded, renumbered, removed, _failed, _errors, nothing, _details, attention = self._sync_report([run], False)
        if nothing:
            return "Nothing to download"
        return f"{downloaded} downloaded{attention}"

    def _on_add_item_finished(self, results: list) -> None:
        item = self._add_current
        if item is None:
            return
        run = results[0] if results else None
        item.run = run
        if run is not None and run.error:
            item.state, item.text = "sync_failed", f"Added, but its sync failed: {run.error}"
        else:
            item.state = "added"
            item.text = self._run_result_line(run) if run is not None else ""
        self._next_add_item()

    def _forget_syncing_status(self, item: QueuedAdd) -> None:
        """A stopped or broken sync: the row shows its cached status again."""
        key = ui_settings.playlist_key(item.entry) if item.entry else None
        if key and self._status.get(key, {}).get("state") in ("syncing", "checking"):
            self._status[key] = {"state": "unknown"}
            row = self._rows.get(key)
            if row is not None:
                self._apply_cached_status(row)
            self.refresh_status_card()

    def _on_add_item_cancelled(self, partial: list) -> None:
        item = self._add_current
        if item is None:
            return
        item.run = partial[0] if partial else None
        item.state, item.text = "stopped", "Added; its sync was stopped. Sync Now finishes it."
        self._forget_syncing_status(item)
        self._next_add_item()

    def _on_add_item_task_failed(self, message: str) -> None:
        item = self._add_current
        if item is None:
            return
        item.state, item.text = "sync_failed", f"Added, but something went wrong: {message}"
        self._forget_syncing_status(item)
        self._next_add_item()

    def _next_add_item(self) -> None:
        """Add and sync the next playlist waiting, or end the run. One that
        add_playlist() refuses (e.g. its folder was deleted meanwhile) is
        marked "Not added" with the reason, and the next one goes."""
        self._add_current = None
        while not self._adds_cancel_all:
            item = next((i for i in self._adds if i.state == "waiting"), None)
            if item is None:
                break
            data = item.data
            try:
                entry = config_manager.add_playlist(
                    self.config, data["source"], data["name"], custom_path=data["custom_path"],
                    artist_id=data.get("artist_id"),  # v0.16.50: an artist's release
                    kind=data.get("kind"),  # v0.16.52: a SoundCloud album
                )
            except ValueError as e:
                item.state, item.text = "not_added", f"Not added: {e}"
                continue
            item.entry, item.state, item.text = entry, "adding", "Preparing…"
            self._add_current = item
            if not self._name_matches(entry["folder_name"]):
                self.search.clear()  # so the new playlist isn't hidden by the search
            self.reload()
            self._launch_add_item(item)
            return
        self._finish_add_run()

    def _on_queue_item_cancel(self, item: QueuedAdd) -> None:
        """✕ on a queue line: a waiting playlist leaves the queue (it was
        never added); the one being added stops at the next safe point
        (it stays added, as after Cancel) and the queue carries on.
        v0.16.36: on Sync Now's list, ✕ stops the playlist being synced."""
        if isinstance(item, QueuedSync):
            self._on_sync_item_stop(item)
            return
        if self._adds is None or item not in self._adds:
            return
        _park_focus(self.panel, self)
        if item.state == "waiting":
            self._adds.remove(item)
            self._refresh_queue_ui()
        elif item.state == "adding" and item is self._add_current:
            if self._control is None or self._control.cancel_requested:
                return
            item.text = "Stopping — finishing the current download…"
            self._control.request_cancel()
            self._refresh_queue_ui()

    def _cancel_all_adds(self) -> None:
        """The panel's Cancel (Cancel all while some are waiting): stop the
        playlist being added and drop the ones waiting."""
        if self._adds is None or self._adds_cancel_all:
            return
        self._adds_cancel_all = True
        for item in self._adds:
            if item.state == "waiting":
                item.state, item.text = "cancelled", "Not added (Cancel all)"
        _park_focus(self.panel, self)
        self._update_add_btn()
        self._refresh_queue_ui()
        if self._control is not None and not self._control.cancel_requested:
            self._control.request_cancel()

    def _finish_add_run(self) -> None:
        """Every playlist of the run is done: the usual Sync Complete (or
        cancelled) summary, and the queue list stays readable until the
        panel is dismissed."""
        items, cancel_all = self._adds or [], self._adds_cancel_all
        self._adds, self._add_current, self._adds_cancel_all = None, None, False
        self._control = None
        self.set_busy(False)
        self.panel.set_bar(self.panel.bar._segments)
        runs = [i.run for i in items if i.run is not None]
        downloaded, renumbered, removed, failed, errors, nothing, details, attention = self._sync_report(runs, cancel_all)
        not_added = [i for i in items if i.state == "not_added"]
        stopped = [i for i in items if i.state == "stopped"]
        added = sum(1 for i in items if i.entry is not None)
        many = len(items) > 1
        if not_added:
            details = "\n".join([details] + [f"=== {i.name} ===\n{i.text}\n" for i in not_added]).strip("\n")
        self._last_sync_report = details
        if cancel_all or (stopped and len(stopped) == added and not not_added):
            text = f"Sync cancelled — {downloaded} downloaded before stopping."
            if many:
                text += f" {added} of {len(items)} playlists added."
            self.panel.show_finished(text, details)
        elif nothing and not (not_added or stopped):
            self.panel.show_finished("Everything is already in sync — nothing to download." if not many else
                                     f"{added} playlists added — nothing to download.", "",
                                     auto_hide_ms=9000, banner="Sync Complete")
        else:
            text = f"{downloaded} downloaded, {renumbered} renumbered, {removed} removed"
            if many:
                text = f"{added} playlist{'s' if added != 1 else ''} added · " + text
            if failed:
                text += attention
            if errors:
                text += f" · {errors} playlist(s) couldn't be synced"
            if not_added:
                text += f" · {len(not_added)} couldn't be added"
            if stopped:
                text += f" · {len(stopped)} stopped"
            quiet = not (failed or errors or not_added or stopped)
            self.panel.show_finished(text, details, auto_hide_ms=15000 if quiet else 0,
                                     banner="Sync Complete")
        self.panel.set_queue(items, live=False)
        self.refresh_status_card()
        self.reload_stats_only()
        if stopped:
            self.refreshRequested.emit()  # a stopped sync leaves the cached numbers stale

    def reload_stats_only(self) -> None:
        for row in self._rows.values():
            row.refresh_text(ui_settings.get_cache(row.key) or None)

    def _show_details(self) -> None:
        ui_dialogs.TextViewerDialog(self, "Sync details", self._last_sync_report or self.panel._details_text).exec()

    def _start_scan(self, entries: List[dict]) -> None:
        if not entries or self.backend.heavy_running or not self._need_library(entries):
            return
        self._running_kind = "scan"
        control = ui_backend.SyncControl(self)
        self._scan_control = control
        self.panel.show_running("Scan Progress:", len(entries), False, True, "Preparing…",
                                cancel_text="Cancel Scan, Use Cache")
        self.set_busy(True)
        self.backend.when_refresh_stopped(lambda: self._launch_scan(entries, control))

    def _launch_scan(self, entries: List[dict], control: ui_backend.SyncControl) -> None:
        if control.cancel_requested:  # cancelled while waiting for the launch-time scan to stop
            self._finish_scan_cancel()
            return
        self._scan_total = len(entries)
        self._scan_done = 0
        thread = self.backend.run(ui_backend.scan_task(self.config, entries, control,
                                                       self.apply_new_releases_call))  # v0.16.50
        thread.entryStarted.connect(self._on_scan_started)
        thread.entryDone.connect(self._on_scan_entry_done)
        thread.resultReady.connect(self._on_scan_finished)
        thread.cancelled.connect(self._on_scan_cancelled)
        thread.failed.connect(self._on_task_failed)

    def _on_scan_started(self, entry: dict) -> None:
        if self._discard_scan_updates:
            return
        self.on_entry_started(entry)
        self.panel.set_text(f"Scanning “{entry['folder_name']}”  |  Playlist {self._scan_done + 1}/{self._scan_total}")

    def _on_scan_entry_done(self, run) -> None:
        if self._discard_scan_updates:
            return
        self._scan_done += 1
        self.panel.set_bar(self._scan_done)
        self.on_entry_done(run)

    def _on_scan_cancelled(self, _partial: list) -> None:
        self._finish_scan_cancel()

    def _on_scan_finished(self, results: list) -> None:
        control, self._scan_control = self._scan_control, None
        if control is not None and control.cancel_requested:
            # Cancel was pressed just as the last playlist finished:
            # still honour it -- cached state, no results page.
            self._finish_scan_cancel()
            return
        self.set_busy(False)
        self.panel.hide_panel()
        self.refresh_status_card()
        self.openResults.emit("scan", results)

    def _start_integrity(self, entries: List[dict]) -> None:
        if not entries or self.backend.heavy_running or not self._need_library(entries):
            return
        self._running_kind = "integrity"
        self.panel.show_running("Integrity Check:", len(entries), False, False, "Checking files…")
        self.set_busy(True)
        self.backend.when_refresh_stopped(lambda: self._launch_integrity(entries))

    def _launch_integrity(self, entries: List[dict]) -> None:
        self._scan_total = len(entries)
        self._scan_done = 0
        thread = self.backend.run(ui_backend.integrity_task(self.config, entries))
        thread.entryStarted.connect(self._on_integrity_started)
        thread.entryDone.connect(self._on_integrity_entry_done)
        thread.resultReady.connect(self._on_integrity_finished)
        thread.failed.connect(self._on_task_failed)

    def _on_integrity_started(self, entry: dict) -> None:
        self.panel.set_text(f"Checking “{entry['folder_name']}”  |  Playlist {self._scan_done + 1}/{self._scan_total}")

    def _on_integrity_entry_done(self, run) -> None:
        self._scan_done += 1
        self.panel.set_bar(self._scan_done)

    def _on_integrity_finished(self, results: list) -> None:
        self.set_busy(False)
        self.panel.hide_panel()
        self.openResults.emit("integrity", results)

    # ------------------------------------------------------------------
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self._remove_mode:
                self._toggle_remove_mode()
                return
            if self._mode:
                self._exit_mode()
                return
        super().keyPressEvent(e)

    # ------------------------------------------------------------------
    # Keyboard control (v0.16.20, Checklist item 27 part 1)
    # ------------------------------------------------------------------
    # ui_keys.KeyboardController calls key_pressed / key_released /
    # key_reset while this page is showing with no overlay or dialog open
    # and no text field being typed in.
    KEY_SCREEN = "main"

    def _key_ring(self) -> Optional[ui_keys.KeyRing]:
        keys = getattr(self.ctx, "keys", None)
        return keys.ring if keys is not None else None

    def _kbd_resolve(self, target: Optional[tuple]) -> Optional[QWidget]:
        if target is None:
            return None
        kind, ref = target
        if kind in ("row", "edit"):
            row = self._rows.get(ref)
            if row is None or row.isHidden():  # hidden by the search (v0.16.30)
                return None
            if kind == "edit":
                return row.edit_btn if row.expanded and row.edit_btn.isVisible() else None
            return row
        return ref if ui_keys.is_valid(ref) and ref.isVisible() else None

    def _kbd_blocked(self) -> bool:
        """The ring isn't drawn while this page is hidden or under the overlay."""
        win = self.window()
        return not self.isVisible() or (hasattr(win, "key_overlay_open") and win.key_overlay_open())

    def _kbd_show(self, target: Optional[tuple], scroll: bool = True) -> bool:
        w = self._kbd_resolve(target)
        ring = self._key_ring()
        if w is None or ring is None:
            return False
        self._kbd_target = target
        if self._kbd_blocked():
            return True
        if scroll and target[0] in ("row", "edit"):
            ensure_visible(self.scroll, w, 0, 10)  # v0.17.0: glides
        ring.show_for(w, 14 if target[0] == "row" else 10, owner=self)
        return True

    def _kbd_default(self) -> tuple:
        shown = self._shown_rows()
        if shown:
            return ("row", shown[0].key)
        return ("w", self.sync_btn if self.sync_btn.isVisible() else self.add_btn)

    def _kbd_current(self) -> Optional[QWidget]:
        """The highlighted control, or None while the ring is hidden."""
        ring = self._key_ring()
        if ring is None or not ring.is_shown(self):
            return None
        return self._kbd_resolve(self._kbd_target)

    def _kbd_wake(self) -> bool:
        """The first key after the mouse was used (or after launch) only
        brings the ring back, on the control it was last on. True if it
        was hidden."""
        if self._kbd_current() is not None:
            return False
        if not self._kbd_show(self._kbd_target):
            self._kbd_show(self._kbd_default())
        return True

    def _on_row_clicked(self, key: str) -> None:
        """A playlist was clicked (v0.16.28, Checklist item 29): put the
        highlight on it as if the keys had moved it there, so its keys
        (E, Enter, Shift+W / Shift+S, ...) work at once. The mouse press
        hid the ring; this runs on the release, so it comes straight back.
        No scrolling: the row is already where the mouse is."""
        self._kbd_show(("row", key), scroll=False)
        # A position box still being typed in would take the keys (E would
        # type into it), and clicking a row doesn't take the focus from it.
        # Leaving the box applies the typed number, as clicking elsewhere
        # always has; deferred so it runs after this click is handled.
        if ui_keys.is_typing_widget(QApplication.focusWidget()):
            QTimer.singleShot(0, self._leave_position_box)

    def _on_row_expand_click(self, key: str, how: str) -> None:
        """A click on a playlist bar outside choosing / Remove mode
        (v0.16.35, Checklist item 36). A left click only selects it
        (_on_row_clicked has put the highlight on it), unless the highlight
        was already on this bar when the mouse went down: then it opens /
        closes, as every left click did before. A right click opens /
        closes it at once. So does a double click, unless its first click
        already did (a selected playlist), so it isn't undone."""
        row = self._rows.get(key)
        if row is None:
            return
        if how == "click":
            ring = self._key_ring()
            selected = ring is not None and ring.target_at_press(self) is row
            self._click_toggled = key if selected else None
            if selected:
                row.toggle_expanded()
        elif how == "double":
            if self._click_toggled != key:
                row.toggle_expanded()
            self._click_toggled = None
        else:
            self._click_toggled = None
            row.toggle_expanded()

    def _leave_position_box(self) -> None:
        fw = QApplication.focusWidget()
        if ui_keys.is_valid(self) and ui_keys.is_typing_widget(fw) and self.isAncestorOf(fw):
            self.setFocus(Qt.OtherFocusReason)

    def _kbd_follow_rebuild(self) -> None:
        """reload() rebuilt the rows: keep the ring on the same playlist."""
        ring = self._key_ring()
        if ring is not None and ring.is_shown(self) and self._kbd_target is not None:
            if not self._kbd_show(self._kbd_target):
                self._kbd_show(self._kbd_default())

    def _kbd_nav_targets(self) -> list:
        """Everything W/A/S/D can move to, as (target, widget)."""
        items = []
        header = getattr(self.window(), "header", None)
        if header is not None:
            items += [header.update_btn, header.cmd_btn, header.gear_btn]
        items += [self.add_btn, self.search, self.expand_all_btn, self.collapse_all_btn]
        out = [(("w", w), w) for w in items]
        for key, row in self._rows.items():
            out.append((("row", key), row))
            if row.expanded:
                out.append((("edit", key), row.edit_btn))
        # v0.16.50: an artist's bar -- its chevron, and when open its buttons
        for arow in self._artist_rows.values():
            out.append((("w", arow.chevron), arow.chevron))
            if arow.expanded:
                out += [(("w", arow.watch_btn), arow.watch_btn), (("w", arow.add_btn), arow.add_btn)]
        for w in (self.remove_btn, self.panel.queue_btn, self.panel.pause_btn, self.panel.cancel_btn,
                  self.panel.details_btn, self.panel.dismiss_btn, self.sync_btn, self.scan_btn,
                  self.integrity_btn, self.toggle_all_btn, self.mode_cancel_btn, self.mode_go_btn,
                  self.change_btn, self.use_cache_btn):
            out.append((("w", w), w))
        for w in self.panel.queue_cancel_buttons():  # v0.16.33: ✕ on a queue line
            out.append((("w", w), w))
        return [(t, w) for t, w in out if w.isEnabled() and ui_keys.on_screen(w)]

    def _kbd_move(self, direction: str) -> None:
        if self._kbd_wake():
            return
        target = self._kbd_target
        cur = self._kbd_resolve(target)
        # W/S go through the playlists in list order, even those scrolled
        # out of view (but not those hidden by the search); holding Space
        # while choosing ticks each one passed.
        if target[0] == "row" and direction in ("up", "down"):
            keys = [r.key for r in self._shown_rows()]
            i = keys.index(target[1]) + (-1 if direction == "up" else 1)
            if 0 <= i < len(keys):
                self._kbd_show(("row", keys[i]))
                if self._space_held and self._mode:
                    self._rows[keys[i]].check.setChecked(self._space_state)
                return
        win = self.window()
        cands = [(t, ui_keys.rect_in(w, win)) for t, w in self._kbd_nav_targets() if t != target]
        pick = ui_keys.spatial_pick(ui_keys.rect_in(cur, win), cands, direction)
        if pick is not None:
            self._kbd_show(pick)

    def _kbd_select(self) -> None:
        """Space: press the highlighted button; on a playlist expand /
        collapse it, or tick it while choosing, or pick it in Remove mode."""
        if self._kbd_wake():
            return
        kind, ref = self._kbd_target
        if kind != "row":
            if ref is self.search:  # Space on the search bar: type in it
                self._kbd_search()
            else:
                ui_keys.press(self._kbd_resolve(self._kbd_target))
            return
        row = self._rows[ref]
        if self._remove_mode:
            self._on_remove_picked(row.entry["folder_name"])
        elif self._mode:
            row.check.toggle()
            self._space_held, self._space_state = True, row.is_checked()
        else:
            row.toggle_expanded()

    def _kbd_number(self, n: int) -> None:
        """1-9 while choosing: tick / untick (or, in Remove mode, pick) the
        playlist at that position, the number in its box. Nothing if the
        search hides it (v0.16.30)."""
        keys = list(self._rows)
        if not 1 <= n <= len(keys):
            return
        row = self._rows[keys[n - 1]]
        if row.isHidden():
            return
        self._kbd_show(("row", keys[n - 1]))
        if self._remove_mode:
            self._on_remove_picked(row.entry["folder_name"])
        else:
            row.check.toggle()

    def _kbd_edit(self) -> None:
        """E: open the Edit page of the highlighted playlist (nothing if
        no playlist is highlighted). Same rule as its Edit button: not
        while a task runs."""
        if self._kbd_current() is None or self._kbd_target[0] not in ("row", "edit"):
            return
        row = self._rows.get(self._kbd_target[1])
        if row is not None and row.edit_btn.isEnabled():
            self._on_edit(row.entry["folder_name"])

    def _kbd_position(self) -> bool:
        """Enter on a highlighted playlist (v0.16.21): type its new position
        in the number box, as if it had been clicked. Enter moves it and
        leaves the box; Esc puts the old number back. Not while a task runs
        (the box is disabled then, as for the mouse)."""
        if self._kbd_current() is None or self._kbd_target[0] != "row":
            return False
        row = self._rows.get(self._kbd_target[1])
        if row is None or not row.number.isEnabled():
            return False
        row.number.setFocus(Qt.ShortcutFocusReason)
        row.number.selectAll()
        return True

    def key_field_escape(self, field: QWidget) -> bool:
        """Esc while typing in a position box: put the current number back
        and leave the box without moving anything. In the search bar
        (v0.16.30): put back the text it had when the typing began (often
        none) and leave it."""
        if field is self.search:
            self.search.setText(self._search_before)
            self.setFocus(Qt.OtherFocusReason)
            return True
        for row in self._rows.values():
            if row.number is field:
                row.number.setText(str(row.position))
                self.setFocus(Qt.OtherFocusReason)
                return True
        return False

    def _kbd_cancel(self) -> None:
        """X: close what is open (Remove mode, the playlist choice), else
        cancel what is running (a sync, a scan, the launch-time scan)."""
        if self._remove_mode:
            self._toggle_remove_mode()
        elif self._mode:
            self._exit_mode()
        elif not ui_keys.press(self.panel.cancel_btn):
            ui_keys.press(self.use_cache_btn)

    def _kbd_shift_move(self, delta: int) -> None:
        """Shift+W / Shift+S (v0.16.22): move the highlighted playlist one
        place up / down by pressing its own up / down arrow, so the mouse's
        rules apply (not past either end, not while a task runs). The
        highlight follows the playlist."""
        if self._kbd_wake() or self._kbd_target[0] != "row":
            return
        row = self._rows.get(self._kbd_target[1])
        if row is not None:
            ui_keys.press(row.up_btn if delta < 0 else row.down_btn)

    def _kbd_dispatch(self, a: set) -> bool:
        if "move_up" in a or "move_down" in a:
            self._kbd_shift_move(-1 if "move_up" in a else 1)
            return True
        for d in ui_keys.MOVE_ACTIONS:
            if d in a:
                self._kbd_move(d)
                return True
        if "select" in a:
            self._kbd_select()
            return True
        if self._mode or self._remove_mode:
            for n in range(1, 10):
                if f"playlist_{n}" in a:
                    self._kbd_number(n)
                    return True
        else:
            for name, btn in (("sync", self.sync_btn), ("scan", self.scan_btn), ("integrity", self.integrity_btn)):
                if name in a:
                    ui_keys.press(btn)
                    return True
        if "start" in a and self._mode:
            return ui_keys.press(self.mode_go_btn)
        if "position" in a and not (self._mode or self._remove_mode):
            return self._kbd_position()
        if "back" in a:
            if self._remove_mode:
                self._toggle_remove_mode()
                return True
            if self._mode:
                self._exit_mode()
                return True
            if self.search.text():  # v0.16.30: then clear the search
                self.search.clear()
                return True
            return False
        if "search" in a:
            self._kbd_search()
            return True
        if "cancel" in a:
            self._kbd_cancel()
            return True
        if "edit" in a:
            self._kbd_edit()
            return True
        header = getattr(self.window(), "header", None)
        buttons = {
            "toggle_all": self.toggle_all_btn, "remove": self.remove_btn, "add": self.add_btn,
            "change_folder": self.change_btn, "pause": self.panel.pause_btn,
            "queue": self.panel.queue_btn,
            "expand_all": self.expand_all_btn, "collapse_all": self.collapse_all_btn,
        }
        if header is not None:
            buttons.update(settings=header.gear_btn, cmd=header.cmd_btn, update=header.update_btn)
        for name, btn in buttons.items():
            if name in a:
                ui_keys.press(btn)
                return True
        return False

    def key_pressed(self, actions: set, _e) -> bool:
        mode_before, remove_before = self._mode, self._remove_mode
        handled = self._kbd_dispatch(actions)
        if handled and ui_keys.is_valid(self):
            self._kbd_after(mode_before, remove_before)
        return handled

    def _kbd_after(self, mode_before: Optional[str], remove_before: bool) -> None:
        """Keep the ring on something sensible after a key's action: the
        first playlist when choosing / Remove mode just opened, the action's
        own button when choosing just closed, else the default."""
        if self._kbd_blocked():
            return
        opened = (self._mode and not mode_before) or (self._remove_mode and not remove_before)
        shown = self._shown_rows()
        if opened and shown:
            self._kbd_show(("row", shown[0].key))
            return
        ring = self._key_ring()
        if ring is None or not ring.is_shown(self) or self._kbd_resolve(self._kbd_target) is not None:
            return
        if mode_before and not self._mode:
            btn = {"sync": self.sync_btn, "scan": self.scan_btn, "integrity": self.integrity_btn}[mode_before]
            if self._kbd_show(("w", btn)):
                return
        self._kbd_show(self._kbd_default())

    def key_released(self, actions: set, _e) -> None:
        if "select" in actions:
            self._space_held = False

    def key_reset(self) -> None:
        self._space_held = False
