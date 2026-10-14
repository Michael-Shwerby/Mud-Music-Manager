"""
Mud Music Manager - Edit page player (split out of ui_edit_page.py in v0.23.4)

EditPagePlayerMixin: the EditPage methods behind the media player at the
bottom of the page (play / pause, previous / next, seek, volume). Local
files only. A mixin, so every method is still an EditPage method.
"""

from typing import List

from PySide6.QtCore import QUrl

try:  # the media backend can be missing on a stripped-down install
    from PySide6.QtMultimedia import QMediaPlayer
except Exception:  # pragma: no cover (the page then has no player)
    pass

import logger
import ui_dialogs
from ui_widgets import format_duration

# Player (v0.16.9): width shared by the track-info and volume columns, so
# the controls between them are centred; starting volume in percent
# (the old fixed setVolume(0.85)).
PLAYER_SIDE_WIDTH = 280
DEFAULT_VOLUME = 85


class EditPagePlayerMixin:
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
