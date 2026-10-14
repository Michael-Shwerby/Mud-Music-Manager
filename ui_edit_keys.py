"""
Mud Music Manager - Edit page keyboard control (split out of ui_edit_page.py in v0.23.4)

EditPageKeysMixin: the EditPage methods ui_keys.KeyboardController calls
(key_pressed / key_released / key_reset) and the song, player and field
shortcuts behind them (Checklist item 27 part 2). A mixin, so every
method is still an EditPage method.
"""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QLineEdit, QWidget

import ui_keys


# Keyboard (v0.16.23): A/D skip, C/V volume step.
SEEK_STEP_MS = 5000
VOLUME_STEP = 5




class EditPageKeysMixin:
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
