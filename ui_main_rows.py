"""
Mud Music Manager - Main Menu bars (split out of ui_main_page.py in v0.23.4)

The playlist / album bar (PlaylistRow), the artist bar (ArtistRow) and the
small helpers they share with the Main Menu and the Edit page: links,
relative times, custom pictures and the open-bar painting. What each bar
does is described in ui_main_page.py's docstring and Design Doc §3.10.
"""

import time
from typing import Optional

from PySide6.QtCore import QRectF, Qt, QUrl, Signal
from PySide6.QtGui import (
    QBrush, QColor, QDesktopServices, QGradient, QIntValidator, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QSpacerItem, QVBoxLayout,
    QWidget,
)

import config_manager
import ui_settings
import artist_watch
from ui_widgets import (
    AlbumBadge, CapsLabel, ClickableElidedLabel, Drawer, DrawerSlide, ElidedLabel, GlyphButton,
    PillLabel, PlatformBadge, RoundCheck, ThumbLabel, format_duration, hseparator, make_button,
    mix_color,
)
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
