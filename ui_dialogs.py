"""
Mud Music Manager - Dialogs (v0.16.2)

Every popup from the navigation map: Add Playlist, Confirm Removal (type
the playlist's name), Repoint-or-Move (Music Folder Change), Exception
Link, Change Playlist Link (Edit page, type the name; v0.16.12), Use your
own file (import an audio file for a song; v0.16.40), plus the small utility dialogs the screens share (unsaved
changes, text viewer for Sync Logs / details, library updates, a busy
dialog for long operations, and simple message helpers).

v0.16.48 (Checklist item 41a): Add accepts a Spotify album link and
fills the Name with "Artist - Album" from a background lookup.

v0.16.50 (Checklist items 41b / 41c): Add also accepts a Spotify artist
link; Download & Sync then opens DiscographyDialog to choose the artist's
releases and whether to keep watching it. ConfirmRemoveArtistDialog
removes an artist (optionally with its albums; files are kept).

v0.16.51 (Checklist item 42a): Add also accepts YT Music playlist, album
and artist links (music.youtube.com); albums and artists work as Spotify
ones do, with the platform named in the hints and the picker.

v0.17.8 (Checklist item 59): ImportDataDialog, the confirmation before
Settings → Your data → Import replaces this computer's data.

v0.19.0 (Checklist item 60b): AppUpdateDialog, shown by Check For Updates
when a newer Mud Music Manager is published: its release notes and a
button opening its download page.

v0.21.0 (Checklist item 60e): in the installed app AppUpdateDialog also has
Update now; UpdateDownloadDialog shows the installer downloading (progress,
Cancel) before the app closes for the installer.

v0.27.0: SupportDialog, opened by the Main Menu's heart button: a "Hello"
banner, Michael's note and his Ko-fi link.

v0.16.24 (Checklist item 27 part 3): keyboard. Each dialog marks its safe
button (Cancel, Keep editing, Not now, No) with safe_first(); ui_keys
starts there, so a stray Space can't delete or change anything. Dialogs
that ask for typing open with the field focused.
"""

import os
import re
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QEasingCurve, QObject, QSize, QThread, Qt, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPixmap, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

import acquisition_engine
import app_info
import config_manager
import downloader
import exceptions_manager
import metadata_tagger
import scheduled_sync
import track_failures
import ui_theme
import youtube_cookies
from ui_widgets import SegmentedProgressBar, Spinner, SupportBanner, animations_on, make_button, make_panel


def _set_role(w: QWidget, role: str) -> None:
    """Change a widget's style role and restyle it (v0.21.0)."""
    w.setProperty("role", role)
    w.style().unpolish(w)
    w.style().polish(w)


# ---------------------------------------------------------------------------
# Base + message helpers
# ---------------------------------------------------------------------------

def safe_first(button: QPushButton) -> QPushButton:
    """Keyboard (v0.16.24): the button W/A/S/D / Space start on in this
    dialog (read by ui_keys)."""
    button.setProperty("kbd_first", True)
    return button


class BaseDialog(QDialog):
    # PySide6 6.x: dialog *instances* don't expose .Accepted/.Rejected, so call
    # sites written as `dlg.exec() == dlg.Accepted` raised AttributeError inside
    # the button handler and the click silently did nothing (Checklist items 8/9).
    # Providing them here keeps every existing call site working.
    Accepted = QDialog.DialogCode.Accepted
    Rejected = QDialog.DialogCode.Rejected

    def __init__(self, parent, title: str, subtitle: str = "", min_width: int = 460, heading: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(min_width)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(26, 24, 26, 22)
        self._root.setSpacing(12)
        if heading:  # v0.27.0: off for a dialog that draws its own (SupportDialog)
            head = QLabel(title)
            head.setProperty("role", "h2")
            f = head.font()
            f.setPointSizeF(13.5)
            f.setBold(True)
            head.setFont(f)
            self._root.addWidget(head)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setProperty("role", "muted")
            sub.setWordWrap(True)
            self._root.addWidget(sub)

    def add_buttons(self, *buttons: QPushButton) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        for b in buttons:
            row.addWidget(b)
        self._root.addSpacing(6)
        self._root.addLayout(row)
        return row


def info(parent, title: str, text: str) -> None:
    box = QMessageBox(QMessageBox.Information, title, text, QMessageBox.Ok, parent)
    box.exec()


def warn(parent, title: str, text: str) -> None:
    box = QMessageBox(QMessageBox.Warning, title, text, QMessageBox.Ok, parent)
    box.exec()


def ask_yes_no(parent, title: str, text: str, yes: str = "Yes", no: str = "No") -> bool:
    box = QMessageBox(QMessageBox.Question, title, text, QMessageBox.NoButton, parent)
    yes_btn = box.addButton(yes, QMessageBox.AcceptRole)
    safe_first(box.addButton(no, QMessageBox.RejectRole))
    box.exec()
    return box.clickedButton() is yes_btn


# ---------------------------------------------------------------------------
# Add Playlist
# ---------------------------------------------------------------------------

_LIVE_ALBUM_LOOKUPS = set()


class _AlbumInfoThread(QThread):
    """Reads a Spotify album's name and artist in the background, for the
    Add dialog's name (v0.16.48, Checklist item 41a). v0.16.52: `fetch`
    reads something else the same way (a SoundCloud set)."""
    done = Signal(str, object, str)  # album id, AlbumInfo or None, error text

    def __init__(self, album_id: str, fetch=None):
        super().__init__()
        self.album_id = album_id
        self._fetch = fetch or acquisition_engine.get_album_info

    def run(self):
        try:
            info, error = self._fetch(self.album_id), ""
        except Exception as e:
            info, error = None, str(e)
        self.done.emit(self.album_id, info, error)


class AddPlaylistDialog(BaseDialog):
    """Add Playlist: link + name, then either 'Download & Sync' (brand-new
    folder under the Music folder) or 'Connect Existing' (a folder of
    songs this app already downloaded and tagged -- asks which folder).

    v0.16.48 (Checklist item 41a): a Spotify album link works too. A
    moment after one is pasted, the album is looked up in the background
    and, if the Name is empty (or still the name filled in for an earlier
    album), the Name becomes "Artist - Album". A line under the link says
    which album it is; an artist link is answered with "not supported yet"."""

    def __init__(self, parent, prefill_link: str = "", prefill_name: str = "", queueing: bool = False):
        # queueing (v0.16.33, Checklist item 34): opened from "Queue Another
        # Playlist" while a playlist is being added; this one waits its turn.
        super().__init__(
            parent, "Queue another playlist" if queueing else "Add a playlist",
            "Paste a Spotify, YT Music, Apple Music, Amazon Music, SoundCloud or YouTube playlist link, "
            "an album link, or a Spotify, YT Music or Apple Music artist link (playlists must be Public)."
            + (" It is added and synced once the playlists ahead of it are done." if queueing else ""),
            min_width=520,
        )
        self.result_data: Optional[dict] = None
        self._album_id: Optional[str] = None   # the album the link names, if any
        self._auto_name = ""                   # the Name this dialog filled in itself
        self._album_timer = QTimer(self)
        self._album_timer.setSingleShot(True)
        self._album_timer.setInterval(500)
        self._album_timer.timeout.connect(self._look_up_album)

        self.link = QLineEdit(prefill_link)
        self.link.setPlaceholderText("https://open.spotify.com/playlist/…  ·  https://music.youtube.com/…  ·  "
                                     "https://www.youtube.com/playlist?list=…")
        self.link_hint = QLabel("")
        self.link_hint.setWordWrap(True)
        self.link_hint.hide()
        self.name = QLineEdit(prefill_name)
        self.name.setPlaceholderText("Name (also the folder name on disk)")

        for label, field in (("Playlist or album link", self.link), ("Name", self.name)):
            cap = QLabel(label)
            cap.setProperty("role", "capsmuted")
            self._root.addWidget(cap)
            self._root.addWidget(field)
            if field is self.link:
                self._root.addWidget(self.link_hint)
        self.link.textChanged.connect(self._on_link_changed)

        cancel = safe_first(make_button("Cancel"))
        cancel.clicked.connect(self.reject)
        connect = make_button("Connect Existing…", "accent")
        connect.setToolTip("Link a folder of songs you already downloaded with this app")
        connect.clicked.connect(self._connect_existing)
        self.connect_btn = connect
        download = make_button("Download && Sync", "primary")
        download.clicked.connect(self._download)
        download.setDefault(True)
        self.add_buttons(cancel, connect, download)
        self._on_link_changed(self.link.text())  # v0.16.50: an artist link greys Name / Connect
        self.link.setFocus()

    # ---- album links (v0.16.48, Checklist item 41a) ----------------------
    def _set_hint(self, text: str, role: str = "muted") -> None:
        self.link_hint.setText(text)
        self.link_hint.setProperty("role", role)
        self.link_hint.style().unpolish(self.link_hint)
        self.link_hint.style().polish(self.link_hint)
        self.link_hint.setVisible(bool(text))

    def _on_link_changed(self, text: str) -> None:
        album_id = config_manager.extract_album_id(text)
        artist_id = config_manager.extract_artist_id(text)
        self._album_timer.stop()
        self._album_id = album_id
        self._album_link = None
        self._kind = None
        # v0.16.52 (Checklist item 42b): a SoundCloud set (or short link) is
        # looked up too: whether it's an album, its name, its full link.
        self._sc_input = text.strip() if (config_manager.extract_soundcloud_set(text)
                                          or config_manager.is_soundcloud_short_link(text)) else None
        if self._sc_input:
            self._set_hint("SoundCloud · looking it up…")
            self._album_timer.start()
        elif album_id:
            self._set_hint(f"{self._platform_of(album_id)} album · looking it up…")
            self._album_timer.start()
        elif artist_id:
            # v0.16.50 (Checklist item 41b): the releases are chosen next.
            self._set_hint(f"{self._platform_of(artist_id)} artist · Download & Sync lets you choose which "
                           "albums and singles to add. Each is named “Artist - Release”, so no name is "
                           "needed here.")
        elif config_manager.extract_ytmusic_playlist_id(text):
            self._set_hint("YT Music playlist")  # v0.16.51
        elif config_manager.extract_apple_music_playlist(text):
            # v0.16.53: songs are found on YouTube, as for Spotify
            self._set_hint("Apple Music playlist · each song is found on YouTube, as for Spotify")
        elif config_manager.extract_amazon_music_playlist(text):
            # v0.16.54 (Checklist item 42d)
            self._set_hint("Amazon Music playlist · each song is found on YouTube, as for Spotify "
                           "(its length is looked up on Deezer or Apple Music first)")
        elif config_manager.detect_source(text) is None and "music.amazon." in text:
            self._set_hint(config_manager.unrecognised_source_message(text), "warn")
        else:
            self._set_hint("")
        artist = bool(config_manager.extract_artist_id(text)) and not album_id
        self.name.setEnabled(not artist)
        if getattr(self, "connect_btn", None) is not None:  # not yet made while prefilling
            self.connect_btn.setEnabled(not artist)

    def _look_up_album(self) -> None:
        if self._sc_input:
            thread = _AlbumInfoThread(self._sc_input, acquisition_engine.get_soundcloud_set_info)
            _LIVE_ALBUM_LOOKUPS.add(thread)
            thread.done.connect(self._on_soundcloud_info)
            thread.finished.connect(lambda t=thread: _LIVE_ALBUM_LOOKUPS.discard(t))
            thread.start()
            return
        if not self._album_id:
            return
        thread = _AlbumInfoThread(self._album_id)
        _LIVE_ALBUM_LOOKUPS.add(thread)
        thread.done.connect(self._on_album_info)
        thread.finished.connect(lambda t=thread: _LIVE_ALBUM_LOOKUPS.discard(t))
        thread.start()

    def _on_soundcloud_info(self, text: str, info, error: str) -> None:
        """v0.16.52: what a SoundCloud link is. An album is added as an
        album ("Artist - Album" filled in, as for any album), a playlist as
        a playlist; a short link is added by its full link."""
        if text != self._sc_input:
            return  # the link changed meanwhile
        if info is None:
            short = config_manager.is_soundcloud_short_link(text)
            self._set_hint("SoundCloud · couldn't look it up right now"
                           + (", so this short link can't be added yet; try again or paste the full link."
                              if short else ", so it's added as a playlist (type a name)."), "warn")
            return
        self._kind = info.kind
        self._album_link = info.link
        tracks = f" · {info.total_tracks} track{'s' if info.total_tracks != 1 else ''}" if info.total_tracks else ""
        if info.is_album:
            year = f" ({info.year})" if info.year else ""
            self._set_hint(f"SoundCloud album: {info.primary_artist} – {info.name}{year}{tracks}")
            current = self.name.text().strip()
            if not current or current == self._auto_name:
                self._auto_name = info.folder_name
                self.name.setText(self._auto_name)
        else:
            self._set_hint(f"SoundCloud playlist: {info.name} by {info.uploader}{tracks}")

    @staticmethod
    def _platform_of(source_id: str) -> str:
        """"Spotify" or "YT Music" for an album / artist ID (v0.16.51)."""
        return config_manager.platform_name(config_manager.id_platform(source_id))

    def _on_album_info(self, album_id: str, info, error: str) -> None:
        if album_id != self._album_id:
            return  # the link changed meanwhile
        platform = self._platform_of(album_id)
        if info is None:
            self._set_hint(f"{platform} album · couldn't look it up right now, so type a name. "
                           "(Adding it still works if the link is right.)", "warn")
            return
        if info.album_id != album_id:
            # v0.16.51: a YT Music album pasted as its own playlist (OLAK5uy_…)
            # is added by its album link, as the artist picker adds it, so
            # the two are recognised as the same album.
            self._album_link = getattr(info, "link", None)
        tracks = f" · {info.total_tracks} track{'s' if info.total_tracks != 1 else ''}" if info.total_tracks else ""
        year = f" ({info.year})" if info.year else ""
        self._set_hint(f"{platform} album: {info.primary_artist} – {info.name}{year}{tracks}")
        current = self.name.text().strip()
        if not current or current == self._auto_name:
            self._auto_name = info.folder_name
            self.name.setText(self._auto_name)

    def _validate(self) -> bool:
        if not self.link.text().strip() or not self.name.text().strip():
            warn(self, "Missing info", "Please enter both a link and a name.")
            return False
        return True

    def _download(self):
        artist_id = config_manager.extract_artist_id(self.link.text())
        if artist_id and not config_manager.extract_album_id(self.link.text()):
            # v0.16.50: an artist; MainPage opens the discography picker next.
            self.result_data = {"mode": "artist", "source": self.link.text().strip(),
                                "artist_id": artist_id, "name": "", "custom_path": None}
            self.accept()
            return
        if not self._validate():
            return
        self.result_data = {"mode": "download", "source": self._source(),
                            "name": self.name.text().strip(), "custom_path": None,
                            "kind": getattr(self, "_kind", None)}  # v0.16.52: a SoundCloud album
        self.accept()

    def _source(self) -> str:
        """The link to add: as pasted, or a YT Music album's own link when
        it was pasted as the album's playlist (v0.16.51)."""
        return getattr(self, "_album_link", None) or self.link.text().strip()

    def _connect_existing(self):
        if not self._validate():
            return
        folder = QFileDialog.getExistingDirectory(self, "Select the folder with your already-downloaded songs")
        if not folder:
            return
        self.result_data = {"mode": "connect", "source": self._source(),
                            "name": self.name.text().strip(), "custom_path": folder,
                            "kind": getattr(self, "_kind", None)}
        self.accept()


# ---------------------------------------------------------------------------
# Artist discography (v0.16.50, Checklist items 41b / 41c)
# ---------------------------------------------------------------------------

_LIVE_DISCOGRAPHY_READS = set()


def fetch_image(url: Optional[str]) -> Optional[bytes]:
    """An image's bytes, or None (never raises)."""
    if not url:
        return None
    try:
        import requests
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.content
    except Exception:
        return None


class _DiscographyThread(QThread):
    done = Signal(object, object, str)  # ArtistInfo or None, picture bytes or None, error

    def __init__(self, artist_id: str):
        super().__init__()
        self.artist_id = artist_id

    def run(self):
        try:
            import artist_watch
            info = artist_watch.read_artist(self.artist_id)
            self.done.emit(info, fetch_image(info.image_url), "")
        except Exception as e:
            self.done.emit(None, None, str(e))


class _ReleaseLine(QWidget):
    """One release in the picker: a tick, its name and "Album · 2001 · 14
    tracks". A release already tracked is ticked, greyed and can't change."""
    toggled = Signal()

    def __init__(self, release, tracked_as: Optional[str], ticked: bool, parent=None):
        super().__init__(parent)
        self.release = release
        self.tracked = tracked_as is not None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(10)
        from ui_widgets import RoundCheck
        self.check = RoundCheck()
        self.check.setChecked(self.tracked or ticked)
        self.check.setEnabled(not self.tracked)
        self.check.toggled.connect(lambda _c: self.toggled.emit())
        lay.addWidget(self.check)
        col = QVBoxLayout()
        col.setSpacing(0)
        title = QLabel(release.name)
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        col.addWidget(title)
        bits = [release.type_text] + ([release.year] if release.year else [])
        if release.total_tracks:
            bits.append(f"{release.total_tracks} track{'s' if release.total_tracks != 1 else ''}")
        if self.tracked:
            bits.append(f"already added as “{tracked_as}”")
        sub = QLabel(" · ".join(bits))
        sub.setProperty("role", "muted")
        col.addWidget(sub)
        lay.addLayout(col, 1)
        if self.tracked:
            title.setEnabled(False)
        self.setCursor(Qt.PointingHandCursor if not self.tracked else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if not self.tracked and e.button() == Qt.LeftButton and not self.check.underMouse():
            self.check.toggle()
        super().mouseReleaseEvent(e)

    def is_chosen(self) -> bool:
        return not self.tracked and self.check.isChecked()


class DiscographyDialog(BaseDialog):
    """Choose which of an artist's releases to add (v0.16.50, Checklist
    item 41b). The artist's Albums and Singles & EPs (Michael's choice) are
    read in the background and listed, newest first, each with a tick:
    all ticked when the artist is new; when it is opened again from the
    artist's bar, only releases never shown before are ticked. Releases
    already tracked are shown ticked and greyed. "Keep watching" (41c,
    ticked by default) makes the app add and download new releases
    automatically with every Scan / Sync.

    After Accepted: self.info (spotify_reader.ArtistInfo), self.chosen
    (the releases to add), self.watch, self.picture (bytes or None)."""

    def __init__(self, parent, artist_id: str, config: dict):
        import artist_watch
        self._platform = artist_watch.platform_name(artist_id)  # v0.16.51: Spotify or YT Music
        super().__init__(parent, "Add an artist", f"Reading the artist from {self._platform}…", min_width=620)
        self._config = config
        self._artist = config_manager.find_artist(config, artist_id)
        self._tracked = {aid: e["folder_name"] for aid, e in artist_watch.tracked_albums(config).items()}
        self.info = None
        self.chosen = []
        self.watch = True if self._artist is None else bool(self._artist.get("watch"))
        self.picture: Optional[bytes] = None
        self._lines: List[_ReleaseLine] = []
        self._sub = self._root.itemAt(1).widget()  # BaseDialog's subtitle label

        busy = QHBoxLayout()
        self._spinner = Spinner(20)
        busy.addWidget(self._spinner)
        busy.addStretch(1)
        self._root.addLayout(busy)
        self._spinner.start()

        from PySide6.QtWidgets import QCheckBox, QScrollArea
        self._tools = QHBoxLayout()
        self._count = QLabel("")
        self._count.setProperty("role", "muted")
        self._tools.addWidget(self._count, 1)
        self._all_btn = make_button("Uncheck all", compact=True)
        self._all_btn.clicked.connect(self._toggle_all)
        self._all_btn.hide()
        self._tools.addWidget(self._all_btn)
        self._root.addLayout(self._tools)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setMinimumHeight(360)
        self._list_host = QWidget()
        self._list = QVBoxLayout(self._list_host)
        self._list.setContentsMargins(0, 0, 8, 0)
        self._list.setSpacing(2)
        self._scroll.setWidget(self._list_host)
        self._scroll.hide()
        self._root.addWidget(self._scroll, 1)

        self.watch_box = QCheckBox("Keep watching this artist: add and download new releases automatically")
        self.watch_box.setChecked(self.watch)
        self.watch_box.hide()
        self._root.addWidget(self.watch_box)

        cancel = safe_first(make_button("Cancel"))
        cancel.clicked.connect(self.reject)
        self.add_btn = make_button("Add", "primary")
        self.add_btn.setEnabled(False)
        self.add_btn.setDefault(True)
        self.add_btn.clicked.connect(self._accept)
        self.add_buttons(cancel, self.add_btn)

        thread = _DiscographyThread(artist_id)
        _LIVE_DISCOGRAPHY_READS.add(thread)
        thread.done.connect(self._on_loaded)
        thread.finished.connect(lambda t=thread: _LIVE_DISCOGRAPHY_READS.discard(t))
        thread.start()

    def _on_loaded(self, info, picture, error: str) -> None:
        self._spinner.stop()
        if info is None:
            self._sub.setText(f"Couldn't read the artist from {self._platform}: {error}")
            self._sub.setProperty("role", "bad")
            self._sub.style().unpolish(self._sub)
            self._sub.style().polish(self._sub)
            return
        self.info, self.picture = info, picture
        known = set((self._artist or {}).get("known_releases") or [])
        self.setWindowTitle(f"Add {info.name}")
        albums = [r for r in info.releases if r.section == "albums"]
        singles = [r for r in info.releases if r.section != "albums"]
        self._sub.setText(
            f"{info.name} · {len(albums)} album{'s' if len(albums) != 1 else ''} · "
            f"{len(singles)} single{'s' if len(singles) != 1 else ''} & EPs. "
            "Each one you tick is added below your playlists as an album (“Artist - Release”) and "
            "downloaded." + (" Releases you skipped before are unticked." if self._artist else ""))
        for caption, group in (("Albums", albums), ("Singles & EPs", singles)):
            if not group:
                continue
            cap = QLabel(f"{caption} ({len(group)})")
            cap.setProperty("role", "capsmuted")
            self._list.addWidget(cap)
            for r in group:
                line = _ReleaseLine(r, self._tracked.get(r.album_id), ticked=r.album_id not in known)
                line.toggled.connect(self._update)
                self._list.addWidget(line)
                self._lines.append(line)
        self._list.addStretch(1)
        self._scroll.show()
        self._all_btn.show()
        self.watch_box.show()
        self._update()

    def _toggle_all(self) -> None:
        free = [l for l in self._lines if not l.tracked]
        on = not all(l.check.isChecked() for l in free)
        for l in free:
            l.check.setChecked(on)

    def _update(self) -> None:
        free = [l for l in self._lines if not l.tracked]
        n = sum(1 for l in free if l.check.isChecked())
        self._count.setText(f"{n} of {len(free)} ticked" + (
            f" · {len(self._lines) - len(free)} already added" if len(free) != len(self._lines) else ""))
        self._all_btn.setText("Uncheck all" if free and n == len(free) else "Check all")
        self.add_btn.setText(f"Add {n} release{'s' if n != 1 else ''}" if n else
                             ("Save" if self._artist else "Add artist only"))
        self.add_btn.setEnabled(self.info is not None)

    def _accept(self) -> None:
        if self.info is None:
            return
        self.chosen = [l.release for l in self._lines if l.is_chosen()]
        self.watch = self.watch_box.isChecked()
        self.accept()


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------

class ConfirmRemoveDialog(BaseDialog):
    """The user must type the playlist's exact name (case-insensitive,
    like the terminal menu) before Remove is enabled. Never deletes
    files -- it only stops tracking the playlist."""

    def __init__(self, parent, playlist_name: str):
        super().__init__(
            parent, "Remove playlist",
            f"This stops syncing “{playlist_name}”. Its downloaded files are NOT deleted.",
        )
        self._name = playlist_name
        cap = QLabel(f"Type the playlist's name to confirm:  {playlist_name}")
        cap.setProperty("role", "capsmuted")
        cap.setWordWrap(True)
        self._root.addWidget(cap)
        self.field = QLineEdit()
        self.field.setPlaceholderText(playlist_name)
        self.field.textChanged.connect(self._check)
        self._root.addWidget(self.field)

        cancel = safe_first(make_button("Cancel"))
        cancel.setAutoDefault(False)  # Enter must not trigger Cancel
        cancel.clicked.connect(self.reject)
        self.remove_btn = make_button("Remove", "danger")
        self.remove_btn.setEnabled(False)
        self.remove_btn.setDefault(True)
        self.remove_btn.clicked.connect(self.accept)
        self.field.returnPressed.connect(self._submit)
        self.add_buttons(cancel, self.remove_btn)
        self.field.setFocus()

    def _check(self, text: str):
        self.remove_btn.setEnabled(text.strip().lower() == self._name.lower())

    def _submit(self):
        # Pressing Enter in the name box == clicking Remove (only once the
        # typed name matches; otherwise nothing happens).
        if self.remove_btn.isEnabled():
            self.accept()


class ConfirmRemoveArtistDialog(ConfirmRemoveDialog):
    """Remove an artist (v0.16.50): stops watching it. "Also remove its
    albums" (ticked) stops tracking its albums too; unticked they stay as
    albums on their own. No file is deleted either way."""

    def __init__(self, parent, artist_name: str, album_count: int):
        super().__init__(parent, artist_name)
        self.setWindowTitle("Remove artist")
        self._root.itemAt(0).widget().setText("Remove artist")
        self._root.itemAt(1).widget().setText(
            f"This stops watching “{artist_name}” for new releases. No downloaded file is deleted.")
        from PySide6.QtWidgets import QCheckBox
        self.albums_box = QCheckBox(
            f"Also remove its {album_count} album{'s' if album_count != 1 else ''} from the list "
            "(their files stay on disk)")
        self.albums_box.setChecked(True)
        self.albums_box.setVisible(album_count > 0)
        self._root.insertWidget(self._root.count() - 2, self.albums_box)


# ---------------------------------------------------------------------------
# Music Folder: repoint or move
# ---------------------------------------------------------------------------

class ChangeFolderDialog(BaseDialog):
    """Repoint-or-Move choice (shown only when a Music folder is already
    set -- with none set there is nothing to move). Returns the mode via
    .mode ('repoint' | 'move'); the caller then opens the folder picker."""

    def __init__(self, parent, current_path: str):
        super().__init__(
            parent, "Change Music folder",
            f"Current folder:  {current_path}",
            min_width=560,
        )
        self.mode: Optional[str] = None

        move = make_button("Move my playlist files there too", "accent")
        move.setToolTip("Playlist folders are moved (crash-safe) to the new location")
        repoint = make_button("Just point the app at the new folder")
        repoint.setToolTip("Existing files stay exactly where they are; only new syncs use the new folder")
        for b, text in (
            (move, "Moves every synced playlist's folder into the new Music folder. Playlists "
                   "linked with “Connect Existing” are never moved."),
            (repoint, "Nothing is moved. The app just looks in the new folder from now on."),
        ):
            self._root.addWidget(b)
            note = QLabel(text)
            note.setProperty("role", "muted")
            note.setWordWrap(True)
            self._root.addWidget(note)

        move.clicked.connect(lambda: self._pick("move"))
        repoint.clicked.connect(lambda: self._pick("repoint"))
        cancel = safe_first(make_button("Cancel"))
        cancel.clicked.connect(self.reject)
        self.add_buttons(cancel)

    def _pick(self, mode: str):
        self.mode = mode
        self.accept()


# ---------------------------------------------------------------------------
# Exception link check (v0.16.38, Checklist item 44)
# ---------------------------------------------------------------------------

# Probe threads still running, kept here (not parented to a dialog) so
# closing the dialog never destroys a running QThread.
_LIVE_PROBES = set()
# (YouTube sign-in setting, video id) -> (title, reason, kind, signed_in),
# so the same link isn't asked twice; changing the sign-in setting
# (v0.16.42) asks again.
_PROBE_CACHE: Dict[tuple, tuple] = {}

# Kinds that say something about the video itself; anything else (no
# connection, bot check, 403...) only means it couldn't be checked now.
_VIDEO_KINDS = (track_failures.AGE_RESTRICTED, track_failures.VIDEO_UNAVAILABLE)


def _probe_key(video_id: str) -> tuple:
    return youtube_cookies.signature(), video_id


class _ProbeThread(QThread):
    done = Signal(str, object, object, object, object, bool)  # video id, cache key, title, reason, kind, signed in

    def __init__(self, video_id: str):
        super().__init__()
        self.video_id = video_id
        self.key = _probe_key(video_id)

    def run(self):
        try:
            title, reason, kind, signed_in = downloader.probe_video(
                f"https://www.youtube.com/watch?v={self.video_id}")
        except Exception as e:  # probe_video never raises; belt and braces
            title, reason, kind, signed_in = None, str(e), track_failures.DOWNLOAD_ERROR, False
        self.done.emit(self.video_id, self.key, title, reason, kind, signed_in)


def _short_reason(reason: str) -> str:
    """yt-dlp's message without its "[youtube] <id>: " prefix and its
    advice about cookies."""
    text = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", reason or "")
    return text.split(" Use --cookies")[0].strip() or "unknown reason"


def link_check_message(title, reason, kind, signed_in: bool = False) -> Tuple[str, str]:
    """(text, label role) describing a probe result."""
    if reason is None:
        if signed_in:
            return (f"✓ Found “{title}”. It can be downloaded signed in "
                    f"({youtube_cookies.describe()}, YouTube sign-in in Settings)."), "ok"
        return f"✓ Found “{title}”. It can be downloaded.", "ok"
    if kind == track_failures.AGE_RESTRICTED:
        if youtube_cookies.is_on():  # v0.16.42: the signed-in retry was tried too
            return (f"⚠ This video is age-restricted on YouTube and couldn't be opened signed in either "
                    f"({_short_reason(reason)}). Try Test under Settings → YouTube sign-in, or choose "
                    f"another upload of the song."), "bad"
        return ("⚠ This video is age-restricted on YouTube, so it can't be downloaded anonymously. Turn on "
                "YouTube sign-in in Settings, or choose another upload of the song (e.g. a lyric video or "
                "a different channel)."), "bad"
    if kind == track_failures.VIDEO_UNAVAILABLE:
        return f"⚠ This video can't be downloaded: {_short_reason(reason)}", "bad"
    return (f"Couldn't check this video right now ({track_failures.label(kind)}). "
            f"You can still use it."), "muted"


class LinkChecker(QObject):
    """Checks a YouTube video in the background a moment after a link is
    entered, and reports (video id, text, role) for the latest one only."""
    result = Signal(str, str, str)

    def __init__(self, parent=None, delay_ms: int = 600):
        super().__init__(parent)
        self._wanted: Optional[str] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._start)

    def check(self, video_id: Optional[str]) -> bool:
        """Check this video (None: forget the last request). Returns True
        if a background check was started (the caller shows "Checking…");
        a known result is reported at once instead."""
        self._wanted = video_id
        self._timer.stop()
        if not video_id:
            return False
        key = _probe_key(video_id)
        if key in _PROBE_CACHE:
            text, role = link_check_message(*_PROBE_CACHE[key])
            self.result.emit(video_id, text, role)
            return False
        self._timer.start()
        return True

    def _start(self):
        video_id = self._wanted
        if not video_id:
            return
        thread = _ProbeThread(video_id)
        _LIVE_PROBES.add(thread)
        thread.done.connect(self._on_done)
        thread.finished.connect(lambda t=thread: _LIVE_PROBES.discard(t))
        thread.start()

    def _on_done(self, video_id, key, title, reason, kind, signed_in):
        if reason is None or kind in _VIDEO_KINDS:
            _PROBE_CACHE[key] = (title, reason, kind, signed_in)  # only lasting answers
        if video_id != self._wanted:
            return
        text, role = link_check_message(title, reason, kind, signed_in)
        self.result.emit(video_id, text, role)


# ---------------------------------------------------------------------------
# Edit page dialogs
# ---------------------------------------------------------------------------

class ExceptionLinkDialog(BaseDialog):
    """Set / change / remove the exception link for one track. Does not
    save anything itself -- the Edit page keeps it pending until Save.

    v0.16.38 (Checklist item 44): a valid link is checked on YouTube in
    the background, and a line under it says whether it can be
    downloaded (warning if it is age-restricted, private, removed...).
    It's a warning only: the link can still be used.

    v0.16.40 (Checklist item 47): "Use your own file…" closes it with
    action 'import'; the Edit page then asks for the audio file."""

    def __init__(self, parent, track_title: str, current_video_id: Optional[str]):
        super().__init__(
            parent, "Exception link",
            f"Use a specific YouTube video for “{track_title}” instead of the automatic match.",
            min_width=540,
        )
        self.action: Optional[str] = None  # 'save' | 'remove' | 'import'
        self.link: Optional[str] = None

        self.field = QLineEdit(f"https://www.youtube.com/watch?v={current_video_id}" if current_video_id else "")
        self.field.setPlaceholderText("Paste a YouTube link or video ID")
        self.field.textChanged.connect(self._validate)
        self._root.addWidget(self.field)
        self.hint = QLabel("")
        self.hint.setProperty("role", "bad")
        self._root.addWidget(self.hint)
        self.check_label = QLabel("")
        self.check_label.setWordWrap(True)
        self.check_label.hide()
        self._root.addWidget(self.check_label)
        self._checker = LinkChecker(self)
        self._checker.result.connect(self._on_checked)

        cancel = safe_first(make_button("Cancel"))
        cancel.setAutoDefault(False)  # Enter in the link box must not trigger Cancel
        cancel.clicked.connect(self.reject)
        buttons: List[QPushButton] = [cancel]
        if current_video_id:
            remove = make_button("Remove exception", "danger")
            remove.clicked.connect(self._remove)
            buttons.append(remove)
        self.save_btn = make_button("Use this link", "primary")
        self.save_btn.clicked.connect(self._save)
        self.save_btn.setEnabled(bool(current_video_id))
        self.save_btn.setDefault(True)  # Enter saves once the link is valid
        buttons.append(self.save_btn)
        row = self.add_buttons(*buttons)
        own = make_button("Use your own file…")
        own.setAutoDefault(False)
        own.setToolTip("Use an audio file you already have for this song instead of a YouTube video")
        own.clicked.connect(self._import)
        row.insertWidget(0, own)
        self.field.setFocus()
        if current_video_id:
            self._check(current_video_id)

    def _import(self):
        self.action = "import"
        self.accept()

    def _validate(self, text: str):
        text = text.strip()
        video_id = exceptions_manager.extract_youtube_id(text)
        ok = bool(video_id)
        self.field.setProperty("invalid", bool(text) and not ok)
        self.field.style().unpolish(self.field)
        self.field.style().polish(self.field)
        self.hint.setText("" if ok or not text else "That doesn't look like a YouTube link or video ID.")
        self.hint.setVisible(bool(self.hint.text()))
        self.save_btn.setEnabled(ok)
        self._check(video_id)

    def _check(self, video_id: Optional[str]):
        if self._checker.check(video_id):
            self._show_check("Checking the video on YouTube…", "muted")
        elif not video_id:
            self.check_label.hide()

    def _on_checked(self, _video_id: str, text: str, role: str):
        self._show_check(text, role)

    def _show_check(self, text: str, role: str):
        self.check_label.setText(text)
        self.check_label.setProperty("role", role)
        self.check_label.style().unpolish(self.check_label)
        self.check_label.style().polish(self.check_label)
        self.check_label.show()

    def _save(self):
        self.action, self.link = "save", self.field.text().strip()
        self.accept()

    def _remove(self):
        self.action = "remove"
        self.accept()


# ---------------------------------------------------------------------------
# Import your own audio file (v0.16.40, Checklist item 47)
# ---------------------------------------------------------------------------

AUDIO_FILE_FILTER = ("Audio files (*.m4a *.mp3 *.flac *.wav *.ogg *.opus *.aac *.wma *.aiff *.aif *.alac "
                     "*.ape *.wv *.mka *.webm *.mp4);;All files (*)")


def choose_audio_file(parent, start_dir: str = "") -> Optional[str]:
    """The file picker for an import; None if cancelled."""
    path, _ = QFileDialog.getOpenFileName(parent, "Choose an audio file", start_dir, AUDIO_FILE_FILTER)
    return path or None


class ImportFileDialog(BaseDialog):
    """Confirm an import of the user's own audio file for one song. Does
    not import anything itself -- the Edit page keeps it pending until
    Save. Says how the file will be handled (copied / converted, original
    left alone) and, for a song Spotify gives no details for (spacer),
    asks for its title and artist (album / year optional), pre-filled
    from the file's own tags. "Choose another file…" swaps the file."""

    def __init__(self, parent, path: str, song_title: str, song_artist: str, spacer: bool,
                 prefill: Optional[Dict[str, str]] = None, replaces: str = ""):
        super().__init__(
            parent, "Use your own file",
            "This song has no details on Spotify, so type its title and artist. They're filled in "
            "from the file's own tags where it has them." if spacer else
            f"Use an audio file you already have for “{song_title}”"
            f"{f' by {song_artist}' if song_artist else ''}. It is tagged with Spotify's details "
            "(title, artist, album, year, cover, Track #) and lyrics, like a download.",
            min_width=560,
        )
        self.spacer = spacer
        self.path = path
        self._no_ffmpeg = False
        self.file_label = QLabel("")
        self.file_label.setWordWrap(True)
        f = self.file_label.font()
        f.setBold(True)
        self.file_label.setFont(f)
        self._root.addWidget(self.file_label)
        self.how_label = QLabel("")
        self.how_label.setWordWrap(True)
        self._root.addWidget(self.how_label)

        self.fields: Dict[str, QLineEdit] = {}
        if spacer:
            for key, label, required in (("title", "Title", True), ("artist", "Artist", True),
                                         ("album", "Album (optional)", False), ("year", "Year (optional)", False)):
                cap = QLabel(label)
                cap.setProperty("role", "muted")
                self._root.addWidget(cap)
                edit = QLineEdit((prefill or {}).get(key, ""))
                if required:
                    edit.textChanged.connect(self._validate)
                self._root.addWidget(edit)
                self.fields[key] = edit
        if replaces:
            note = QLabel(replaces)
            note.setProperty("role", "warn")
            note.setWordWrap(True)
            self._root.addWidget(note)

        cancel = safe_first(make_button("Cancel"))
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        self.ok_btn = make_button("Use this file", "primary")
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self.accept)
        row = self.add_buttons(cancel, self.ok_btn)
        other = make_button("Choose another file…")
        other.setAutoDefault(False)
        other.clicked.connect(self._choose_other)
        row.insertWidget(0, other)
        self._show_file()
        if spacer:
            self.fields["title"].setFocus()

    def _show_file(self) -> None:
        name = os.path.basename(self.path)
        self.file_label.setText(f"{name}\n{os.path.dirname(self.path)}")
        is_m4a = self.path.lower().endswith(".m4a")
        no_ffmpeg = not is_m4a and not downloader.ffmpeg_path()
        if no_ffmpeg:
            text, role = ("ffmpeg wasn't found, so this file can't be converted. Choose an .m4a file, "
                          "or install ffmpeg."), "bad"
        elif is_m4a:
            text, role = "It is copied into the playlist as it is. The original file stays where it is.", "muted"
        else:
            text, role = (f"It is converted to .m4a (AAC, {downloader.IMPORT_AAC_BITRATE.rstrip('k')} kbps) "
                          "with ffmpeg. The original file stays where it is."), "muted"
        self.how_label.setText(text)
        self.how_label.setProperty("role", role)
        self.how_label.style().unpolish(self.how_label)
        self.how_label.style().polish(self.how_label)
        self._no_ffmpeg = no_ffmpeg
        self._validate()

    def _choose_other(self) -> None:
        path = choose_audio_file(self, os.path.dirname(self.path))
        if path:
            self.path = path
            if self.spacer:  # fill only the fields still empty from the new file's tags
                tags = metadata_tagger.read_source_tags(path)
                for key, edit in self.fields.items():
                    if not edit.text().strip() and tags.get(key):
                        edit.setText(tags[key])
            self._show_file()

    def _validate(self, *_args) -> None:
        ok = not self._no_ffmpeg
        if self.spacer:
            ok = ok and bool(self.fields["title"].text().strip()) and bool(self.fields["artist"].text().strip())
        self.ok_btn.setEnabled(ok)

    def value(self, key: str) -> str:
        edit = self.fields.get(key)
        return edit.text().strip() if edit else ""


class ConfirmLinkChangeDialog(BaseDialog):
    """Changing the playlist's own link on Save (Checklist item 16). Says
    plainly that the folder's current songs are removed on the next sync,
    and -- like Remove -- the playlist's name must be typed first."""

    def __init__(self, parent, playlist_name: str, old_link: str, new_link: str,
                 old_platform: str, new_platform: str):
        super().__init__(
            parent, "Change playlist link",
            f"“{playlist_name}” will follow a different playlist from now on.",
            min_width=560,
        )
        self._name = playlist_name

        for caption, link in (("Current link", old_link), ("New link", new_link)):
            cap = QLabel(caption)
            cap.setProperty("role", "capsmuted")
            self._root.addWidget(cap)
            value = QLabel(link)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._root.addWidget(value)

        lines = []
        if old_platform != new_platform:
            lines.append(f"This switches the playlist from {old_platform} to {new_platform}.")
        lines.append(
            "On the next sync, every song currently in this playlist's folder will be "
            "removed, because none of them belong to the new playlist. The new playlist's "
            "songs are then downloaded fresh."
        )
        lines.append(
            "Exceptions saved for the current playlist are kept, but they won't apply to "
            "the new one."
        )
        warning = QLabel("\n\n".join(lines))
        warning.setProperty("role", "bad")
        warning.setWordWrap(True)
        self._root.addSpacing(4)
        self._root.addWidget(warning)

        cap = QLabel(f"Type the playlist's name to confirm:  {playlist_name}")
        cap.setProperty("role", "capsmuted")
        cap.setWordWrap(True)
        self._root.addSpacing(4)
        self._root.addWidget(cap)
        self.field = QLineEdit()
        self.field.setPlaceholderText(playlist_name)
        self.field.textChanged.connect(self._check)
        self._root.addWidget(self.field)

        cancel = safe_first(make_button("Cancel"))
        cancel.setAutoDefault(False)  # Enter must not trigger Cancel
        cancel.clicked.connect(self.reject)
        self.change_btn = make_button("Change link", "danger")
        self.change_btn.setEnabled(False)
        self.change_btn.setDefault(True)
        self.change_btn.clicked.connect(self.accept)
        self.field.returnPressed.connect(self._submit)
        self.add_buttons(cancel, self.change_btn)
        self.field.setFocus()

    def _check(self, text: str):
        self.change_btn.setEnabled(text.strip().lower() == self._name.lower())

    def _submit(self):
        if self.change_btn.isEnabled():
            self.accept()


class UnsavedChangesDialog(BaseDialog):
    """Back with unsaved edits: Save / Discard / Keep editing."""

    def __init__(self, parent):
        super().__init__(parent, "Unsaved changes", "You have changes on this page that haven't been saved yet.")
        self.choice = "cancel"
        keep = safe_first(make_button("Keep editing"))
        keep.clicked.connect(self.reject)
        discard = make_button("Discard changes", "danger")
        discard.clicked.connect(lambda: self._set("discard"))
        save = make_button("Save", "primary")
        save.clicked.connect(lambda: self._set("save"))
        save.setDefault(True)
        self.add_buttons(keep, discard, save)

    def _set(self, choice: str):
        self.choice = choice
        self.accept()


# ---------------------------------------------------------------------------
# Utility dialogs
# ---------------------------------------------------------------------------

class TextViewerDialog(BaseDialog):
    """Read-only text (Sync Logs, run details)."""

    def __init__(self, parent, title: str, text: str, subtitle: str = "", mono: bool = True):
        super().__init__(parent, title, subtitle, min_width=680)
        self.setMinimumHeight(460)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        if mono:
            view.setFont(ui_theme.mono_font(9.5))
        self._root.addWidget(view, 1)
        close = make_button("Close", "primary")
        close.clicked.connect(self.accept)
        self.add_buttons(close)


SUPPORT_TEXT = (
    "What started as a simple proof of concept has morphed into a solid tool that I use regularly. "
    "I figured this tool could be useful to others out there, who are also interested in having their "
    "music in file form.",

    "Since this is my first time releasing/maintaining a public tool, I'm definitely learning as I go! "
    "Still though, I plan to keep MMM completely free, open-source, updated, and available for the "
    "foreseeable future (or as long as I can reasonably do so).",

    "As the lone maintainer of the project, anything helps! If you're enjoying the app, please consider "
    "supporting me on Ko-Fi or contributing to the code yourself on GitHub! If you'd like to contribute "
    "to the project's code or request a feature please see the contributing section on GitHub. Thank you.",
)

# The note's face: warm and rounded but as easy to read as the UI font;
# the first one installed is used (Candara ships with Windows).
NOTE_FAMILIES = ["Candara", "Corbel", "Avenir Next", "Segoe UI", "Arial"]


class SupportDialog(BaseDialog):
    """The Main Menu's heart button (v0.27.0): a gradient "Hello" banner
    with a few drifting hearts (ui_widgets.SupportBanner), Michael's note
    on a card in a friendly, readable face (Candara), the Ko-fi link (Ko-fi's cup logo) as a
    pill, and a big gradient "Support MMM ❤" whose glow beats gently.
    Both links open app_info.KOFI_URL in the browser; the window stays
    open."""

    def __init__(self, parent):
        super().__init__(parent, f"Support {app_info.APP_NAME}", min_width=660, heading=False)
        self._root.setSpacing(16)
        self._root.addWidget(SupportBanner("Hello"))

        card = make_panel()
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(26, 20, 26, 22)
        c_lay.setSpacing(12)
        note_font = QFont()
        note_font.setFamilies(NOTE_FAMILIES)
        note_font.setPointSizeF(13)
        for para in SUPPORT_TEXT:
            text = QLabel(para)
            text.setWordWrap(True)
            text.setFont(note_font)
            c_lay.addWidget(text)
        self._root.addWidget(card)

        kofi = make_button("  " + app_info.KOFI_URL.split("://", 1)[-1], "link")
        kofi.setToolTip(f"Open {app_info.KOFI_URL} in your browser")
        icon = QPixmap(app_info.KOFI_ICON_PATH)
        if not icon.isNull():
            kofi.setIcon(QIcon(icon))
            kofi.setIconSize(QSize(34, 28))
        kofi.clicked.connect(self._open_kofi)
        self._root.addWidget(kofi, 0, Qt.AlignHCenter)

        support = make_button("Support MMM  ❤", "support")
        support.setToolTip(f"Open {app_info.KOFI_URL} in your browser")
        support.clicked.connect(self._open_kofi)
        # a glow in the heart's colour that swells and fades like a heartbeat
        glow = QGraphicsDropShadowEffect(support)
        glow.setOffset(0, 0)
        glow.setColor(QColor(ui_theme.palette().heart))
        glow.setBlurRadius(28)
        support.setGraphicsEffect(glow)
        if animations_on():
            self._beat = QVariantAnimation(self)
            self._beat.setStartValue(14.0)
            self._beat.setKeyValueAt(0.5, 44.0)
            self._beat.setEndValue(14.0)
            self._beat.setDuration(1600)
            self._beat.setEasingCurve(QEasingCurve.InOutSine)
            self._beat.setLoopCount(-1)
            self._beat.valueChanged.connect(lambda v: glow.setBlurRadius(float(v)))
            self._beat.start()
        self._root.addSpacing(6)
        self._root.addWidget(support, 0, Qt.AlignHCenter)

        close = safe_first(make_button("Close"))
        close.clicked.connect(self.accept)
        self.add_buttons(close)
        # tall enough for the wrapped text at this width (it was cut off otherwise)
        self.setMinimumHeight(self._root.totalHeightForWidth(self.minimumWidth()))

    def _open_kofi(self) -> None:
        QDesktopServices.openUrl(QUrl(app_info.KOFI_URL))


class ImportDataDialog(BaseDialog):
    """Settings → Your data → Import (v0.17.8, Checklist item 59): what the
    backup holds, what it replaces, and where the Music folder will be.
    Cancel is the safe first button; Import replaces the data."""

    def __init__(self, parent, backup, library_path: Optional[str], edit_open: bool = False,
                 edit_unsaved: bool = False):
        super().__init__(
            parent, "Import your data",
            f"Backup made {backup.created_text()} by Mud Music Manager v{backup.app_version}.",
            min_width=560,
        )
        holds = QLabel(f"{backup.counts.describe()}, and your settings.")
        holds.setWordWrap(True)
        self._root.addWidget(holds)

        warn_text = ("This replaces the playlists, albums, artists, exceptions and settings on this computer "
                     "with the backup's. Your music files are not touched. Your current data is saved first, "
                     "so you can go back by importing that copy.")
        if edit_unsaved:
            warn_text += "\n\nThe Edit page closes, and its unsaved changes are lost."
        elif edit_open:
            warn_text += "\n\nThe Edit page closes."
        replaces = QLabel(warn_text)
        replaces.setProperty("role", "warn")
        replaces.setWordWrap(True)
        self._root.addWidget(replaces)

        theirs = backup.config.get("library_path")
        if library_path and library_path == theirs:
            folder = f"Music folder: {library_path} (the backup's, found on this computer)."
        elif library_path:
            folder = f"Music folder: {library_path} (this computer's, kept)."
        else:
            folder = ("Music folder: none yet. Choose one on the Main Menu (Music Folder → Change) "
                      "after the import.")
        folder_label = QLabel(folder)
        folder_label.setProperty("role", "muted")
        folder_label.setWordWrap(True)
        self._root.addWidget(folder_label)

        cancel = safe_first(make_button("Cancel"))
        cancel.clicked.connect(self.reject)
        go = make_button("Import", "primary")
        go.clicked.connect(self.accept)
        self.add_buttons(cancel, go)


class UpdatesDialog(BaseDialog):
    """Offers to pip-update outdated libraries (same set the terminal
    menu's Update Project Libraries offers)."""

    def __init__(self, parent, outdated: list):
        super().__init__(
            parent, "Library updates available",
            f"{len(outdated)} project librar{'y has' if len(outdated) == 1 else 'ies have'} a newer version.",
        )
        lines = "\n".join(f"•  {s.name}:  {s.installed_version}  →  {s.latest_version}" for s in outdated)
        body = QLabel(lines)
        self._root.addWidget(body)
        note = QLabel("Updates take effect after you restart Mud Music Manager.")
        note.setProperty("role", "muted")
        self._root.addWidget(note)
        later = safe_first(make_button("Not now"))
        later.clicked.connect(self.reject)
        update = make_button("Update now", "primary")
        update.clicked.connect(self.accept)
        self.add_buttons(later, update)


def _recolour_links(document, colour: QColor) -> None:
    """Qt's Markdown reader colours links its own fixed blue, unreadable on
    the dark theme: give every link the theme's link colour instead."""
    block = document.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.charFormat().isAnchor():
                cursor = QTextCursor(document)
                cursor.setPosition(frag.position())
                cursor.setPosition(frag.position() + frag.length(), QTextCursor.KeepAnchor)
                fmt = QTextCharFormat()
                fmt.setForeground(colour)
                cursor.mergeCharFormat(fmt)
            it += 1
        block = block.next()


def _installed_for_all_users() -> bool:
    try:
        import app_update
        return app_update.installed_for_all_users()
    except Exception:
        return False


class AppUpdateDialog(BaseDialog):
    """A newer Mud Music Manager is published on GitHub (v0.19.0, Checklist
    item 60b): this version → the new one, its release notes, and a button
    that opens its download page in the browser. Nothing is installed from
    here yet (Checklist item 60e)."""

    def __init__(self, parent, status):
        when = f", released {status.published}" if status.published else ""
        super().__init__(
            parent, "A new version is available",
            f"Mud Music Manager v{status.latest}{when}. You have v{status.current}.",
            min_width=620,
        )
        self.setMinimumHeight(420)
        heading = QLabel("What's new")
        heading.setProperty("role", "capsmuted")
        self._root.addWidget(heading)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        if status.notes:
            notes.setMarkdown(status.notes)
            _recolour_links(notes.document(), QColor(ui_theme.palette().link))
        else:
            notes.setPlainText("This release has no notes.")
        self._root.addWidget(notes, 1)
        # v0.21.0 (Checklist item 60e): the installed app can update itself.
        installable = status.can_install()
        all_users = installable and _installed_for_all_users()
        how = QLabel(
            "Update now downloads it, checks it and installs it: Mud Music Manager closes and reopens by "
            "itself. Your playlists, settings and music stay as they are."
            + (" This copy is installed for all users, so Windows will ask for administrator permission."
               if all_users else "")
            if installable else
            "Download it from its page on GitHub. Your playlists, settings and music stay as they are.")
        how.setProperty("role", "muted")
        how.setWordWrap(True)
        self._root.addWidget(how)
        self._url = status.page_url
        self.choice: Optional[str] = None  # "update", "page" or None
        later = safe_first(make_button("Not now"))
        later.clicked.connect(self.reject)
        page = make_button("Open download page", "ghost" if installable else "primary")
        page.setToolTip(self._url)
        page.clicked.connect(self._open_page)
        if installable:
            size = f" ({status.installer_size / 1_048_576:.0f} MB)" if status.installer_size else ""
            update = make_button("Update now", "primary")
            update.setToolTip(f"Download and install v{status.latest}{size}")
            update.clicked.connect(self._update_now)
            self.add_buttons(later, page, update)
        else:
            self.add_buttons(later, page)

    def _open_page(self) -> None:
        QDesktopServices.openUrl(QUrl(self._url))
        self.choice = "page"
        self.accept()

    def _update_now(self) -> None:
        self.choice = "update"
        self.accept()


class UpdateDownloadDialog(BaseDialog):
    """Update now (v0.21.0, Checklist item 60e): the installer downloading,
    with a progress bar and Cancel, then "starting the installer". The
    download itself runs on a background thread (ui_backend.download_update_task);
    the window calls on_cancel when Cancel is pressed and closes itself only
    once that thread has ended."""

    def __init__(self, parent, status, on_cancel):
        super().__init__(parent, f"Updating to v{status.latest}",
                         "Downloading the installer from GitHub…", min_width=520)
        self._on_cancel = on_cancel
        self._finished = False
        self.message = QLabel("Starting the download…")
        self.message.setProperty("role", "muted")
        self.message.setWordWrap(True)
        self._root.addWidget(self.message)
        self.bar = SegmentedProgressBar()
        self.bar.set_segments(1)
        self._root.addWidget(self.bar)
        self.cancel_btn = safe_first(make_button("Cancel"))
        self.cancel_btn.clicked.connect(self._cancel)
        self.add_buttons(self.cancel_btn)

    def on_progress_text(self, text: str) -> None:
        """'done/total' in bytes, from the download thread."""
        try:
            done, total = (int(x) for x in text.split("/"))
        except ValueError:
            return
        mb = done / 1_048_576
        if total:
            self.bar.set_value(min(done / total, 1.0))
            self.message.setText(f"{mb:.0f} of {total / 1_048_576:.0f} MB")
        else:
            self.message.setText(f"{mb:.0f} MB")

    def set_message(self, text: str) -> None:
        self.message.setText(text)

    def starting_installer(self) -> None:
        self.bar.set_value(1.0)
        self.message.setText("Checked. Starting the installer: Mud Music Manager closes now and reopens "
                             "by itself when the update is installed.")
        self.cancel_btn.setEnabled(False)

    def failed(self, message: str) -> None:
        """The download or check failed (or was cancelled): say why; the
        button becomes Close."""
        self._finished = True
        if message.startswith("UpdateError: "):  # TaskThread prefixes the exception's type
            message = message[len("UpdateError: "):]
        self.message.setText(message)
        _set_role(self.message, "bad")
        self.cancel_btn.setText("Close")
        self.cancel_btn.setEnabled(True)

    def _cancel(self) -> None:
        if self._finished:
            self.accept()
            return
        self._on_cancel()
        self.cancel_btn.setEnabled(False)
        self.message.setText("Cancelling…")

    def reject(self):  # Esc / the window's X: cancel first, close once the download has stopped
        self._cancel()


class BackgroundSyncDialog(BaseDialog):
    """v0.25.0 (Checklist item 65): the app was opened while a scheduled
    sync runs in the background (scheduled_sync.background_run()). Two
    copies must never sync at once, so the app waits: this window follows
    the background run's progress and opens the app when it ends. Stop
    asks it to stop at its next safe point (the song downloading finishes,
    as Cancel does). Close (or Esc) quits instead of opening the app.
    exec() returns Accepted when the app may open."""

    def __init__(self, parent=None):
        super().__init__(parent, "A scheduled sync is running",
                         "Your playlists are being synced in the background. Mud Music Manager opens when it "
                         "finishes, so the two never change your music at the same time.", min_width=520)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.spinner = Spinner(22)
        row.addWidget(self.spinner, 0, Qt.AlignVCenter)
        self.spinner.start()
        self.message = QLabel("")
        self.message.setProperty("role", "muted")
        self.message.setWordWrap(True)
        row.addWidget(self.message, 1)
        self._root.addLayout(row)
        self.stop_btn = make_button("Stop it and open the app")
        self.stop_btn.clicked.connect(self._stop)
        self.quit_btn = safe_first(make_button("Close", "ghost"))
        self.quit_btn.clicked.connect(self.reject)
        self.add_buttons(self.quit_btn, self.stop_btn)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(1000)
        self._poll()

    def _poll(self) -> None:
        state = scheduled_sync.background_run()
        if state is None:
            self._timer.stop()
            self.accept()
            return
        where = state.get("playlist")
        count = f" ({state.get('index')} of {state.get('total')})" if state.get("total") else ""
        text = f"Syncing ‘{where}’{count}." if where else "Starting…"
        if state.get("current"):
            text += "\n" + state["current"]
        if state.get("stop"):
            text = "Stopping after the current song…\n" + text
        self.message.setText(text)

    def _stop(self) -> None:
        scheduled_sync.request_stop()
        self.stop_btn.setEnabled(False)
        self._poll()


class BusyDialog(QDialog):
    """Modal 'please wait' with a spinner and a changing message, for
    operations the user must not interleave with others (moving the
    Music folder, saving Edit-page changes). Not closable by the user."""

    def __init__(self, parent, title: str, message: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 24, 26, 24)
        lay.setSpacing(14)
        head = QHBoxLayout()
        self.spinner = Spinner(22)
        head.addWidget(self.spinner)
        t = QLabel(title)
        f = t.font()
        f.setPointSizeF(13.5)
        f.setBold(True)
        t.setFont(f)
        head.addWidget(t, 1)
        lay.addLayout(head)
        self.message = QLabel(message)
        self.message.setProperty("role", "muted")
        self.message.setWordWrap(True)
        lay.addWidget(self.message)
        self.spinner.start()

    def set_message(self, text: str) -> None:
        self.message.setText(text)

    def reject(self):  # Esc must not close it
        pass

    def finish(self):
        self.spinner.stop()
        QDialog.accept(self)
