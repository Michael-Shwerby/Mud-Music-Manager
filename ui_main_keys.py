"""
Mud Music Manager - Main Menu keyboard control (split out of ui_main_page.py in v0.23.4)

MainPageKeysMixin: the MainPage methods that ui_keys.KeyboardController
calls (key_pressed / key_released / key_reset) and the W/A/S/D movement,
selection and shortcuts behind them (Checklist item 27). A mixin, so
every method is still a MainPage method (self is the page).
"""

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget

import ui_keys
from ui_widgets import ensure_visible


class MainPageKeysMixin:
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
            items += [header.update_btn]
            # v0.28.0: the version pill (opens the update details) and Update &
            # Restart MMM, when shown
            items += [w for w in (header.version_pill, header.restart_btn) if w.isVisible()]
            items += [header.cmd_btn, header.gear_btn]
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
                  self.integrity_btn, self.copy_btn, self.toggle_all_btn, self.mode_cancel_btn, self.mode_go_btn,
                  self.change_btn, self.use_cache_btn, self.support_btn):  # v0.27.0: the heart
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
            for name, btn in (("sync", self.sync_btn), ("scan", self.scan_btn), ("integrity", self.integrity_btn),
                              ("copy", self.copy_btn)):  # v0.26.0
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
            btn = {"sync": self.sync_btn, "scan": self.scan_btn, "integrity": self.integrity_btn,
                   "copy": self.copy_btn}[mode_before]
            if self._kbd_show(("w", btn)):
                return
        self._kbd_show(self._kbd_default())

    def key_released(self, actions: set, _e) -> None:
        if "select" in actions:
            self._space_held = False

    def key_reset(self) -> None:
        self._space_held = False
