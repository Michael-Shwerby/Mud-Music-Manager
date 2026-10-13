"""
Mud Music Manager - Remap keyboard controls (v0.16.26, Checklist item 27 part 5)

Opened from Settings -> Keyboard -> Remap keyboard controls. Three
sections: Main Menu, Edit page and Misc (Settings / results, dialogs).
Every key can be changed: click a key and press the new one, + adds
another key, x removes one. Changes take effect on Apply and are stored in
ui_settings.json ("keymap", only what differs from the defaults);
playlists.json is never touched.

Safeguards (Michael, 2026-10-07: only for the keys whose misbinding
would seriously get in the way -- movement, selection, Back; see
ui_keys.MAJOR_ACTIONS):
  - those always keep at least one key (their last key can't be removed);
  - if Apply changes any of them, a "Keep these keys?" box counts down 15
    seconds with the new keys already working; unless "Keep these keys" is
    pressed, the old keys come back by themselves.
Other keys apply straight away. Reset to defaults applies at once (the
defaults are known to work). Two actions of one screen can't share a key
(the window offers to swap), except pairs that are never live together.
Esc always closes Settings, results and dialogs, whatever is bound.
"""

import copy
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

import ui_dialogs
import ui_keys
import ui_settings
from ui_widgets import SegmentedControl, make_button

SECTIONS = [("main", "Main Menu"), ("edit", "Edit page"), ("misc", "Misc")]
MISC_GROUPS = [("overlay", "Settings and results"), ("dialog", "Dialogs")]
KEEP_SECONDS = 15


def action_label(section: str, action: str) -> str:
    return ui_keys.ACTION_LABELS.get(section, {}).get(action, action)


def key_text(key: str) -> str:
    """A key as the remap window shows it. Unlike the Tab map, Return and
    the number pad's Enter are told apart here ("Enter" / "Num Enter")."""
    return "Num Enter" if key == "Enter" else ui_keys.key_label(key)


def key_from_event(e) -> str:
    """The key pressed, as keymap text ("W", "Shift+W", "Ctrl+S", "Up").
    "" for a lone modifier. Shift is kept only for letters and named keys
    (Shift+1 gives "!", which is recorded as "!")."""
    k = int(getattr(e.key(), "value", e.key()))
    if k in ui_keys._MODIFIER_KEYS or k in (0, Qt.Key_unknown.value):
        return ""
    mods = int(getattr(e.modifiers(), "value", e.modifiers()))
    parts = []
    if mods & Qt.ControlModifier.value:
        parts.append("Ctrl")
    if mods & Qt.AltModifier.value:
        parts.append("Alt")
    if mods & Qt.MetaModifier.value:
        parts.append("Meta")
    letter = Qt.Key_A.value <= k <= Qt.Key_Z.value
    named = k >= 0x01000000
    if mods & Qt.ShiftModifier.value and (letter or named):
        parts.append("Shift")
    name = QKeySequence(k).toString(QKeySequence.PortableText)
    if not name:
        return ""
    return ui_keys.normalize_key("+".join(parts + [name]))


class KeepKeysDialog(ui_dialogs.BaseDialog):
    """Shown after Apply changed movement, selection or Back keys. The new
    keys already work here, so reaching "Keep these keys" with them proves
    they do. Anything else -- Go back, Esc, closing it, or the countdown
    running out -- puts the old keys back."""

    def __init__(self, parent, changed_names):
        super().__init__(
            parent, "Keep these keys?",
            "You changed: " + ", ".join(changed_names) + ". The new keys are already working; "
            "if they work for you, use them to press “Keep these keys”.",
            min_width=520,
        )
        self.left = KEEP_SECONDS
        self.count = QLabel("")
        self.count.setProperty("role", "warn")
        self._root.addWidget(self.count)
        back = ui_dialogs.safe_first(make_button("Go back to the old keys"))
        back.setDefault(True)  # Enter with nothing highlighted goes back
        back.clicked.connect(self.reject)
        self.keep_btn = make_button("Keep these keys", "primary")
        self.keep_btn.setAutoDefault(False)
        self.keep_btn.clicked.connect(self.accept)
        self.add_buttons(back, self.keep_btn)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._show_count()
        self._timer.start()

    def _show_count(self) -> None:
        self.count.setText(f"The old keys come back in {self.left} second{'s' if self.left != 1 else ''}.")

    def _tick(self) -> None:
        self.left -= 1
        self._show_count()
        if self.left <= 0:
            self._timer.stop()
            self.reject()


class RemapDialog(ui_dialogs.BaseDialog):
    def __init__(self, parent, controller: "ui_keys.KeyboardController"):
        super().__init__(
            parent, "Remap keyboard controls",
            "Click a key to change it (then press the new key), + to add another key, × to remove one. "
            "Changes take effect when you press Apply. Keys marked “safeguarded” always keep a key, and "
            "changing them asks you to confirm within 15 seconds.",
            min_width=760,
        )
        self.setMinimumHeight(600)
        self.controller = controller
        self.applied = controller.keymap.data
        self.work = copy.deepcopy(self.applied)
        self.key_capturing = False      # read by ui_keys.KeyboardController
        self._capture = None            # (section, action, index or None)
        self._section = "main"

        top = QHBoxLayout()
        self.switch = SegmentedControl([(k, t) for k, t in SECTIONS], self._section)
        self.switch.changed.connect(self._switch_section)
        top.addWidget(self.switch)
        top.addStretch(1)
        self._root.addLayout(top)

        status_row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setProperty("role", "muted")
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        self.cancel_capture_btn = make_button("Cancel", compact=True)
        self.cancel_capture_btn.clicked.connect(self._cancel_capture)
        self.cancel_capture_btn.hide()
        status_row.addWidget(self.cancel_capture_btn)
        self._root.addLayout(status_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._root.addWidget(self.scroll, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.reset_btn = make_button("Reset to defaults", "danger")
        self.reset_btn.clicked.connect(self._reset)
        buttons.addWidget(self.reset_btn)
        buttons.addStretch(1)
        self.close_btn = ui_dialogs.safe_first(make_button("Close"))
        self.close_btn.setAutoDefault(False)
        self.close_btn.clicked.connect(self.reject)
        self.apply_btn = make_button("Apply", "primary")
        self.apply_btn.setAutoDefault(False)
        self.apply_btn.clicked.connect(self.apply)
        buttons.addWidget(self.close_btn)
        buttons.addWidget(self.apply_btn)
        self._root.addSpacing(6)
        self._root.addLayout(buttons)

        self._rebuild()

    # ------------------------------------------------------------------
    # Building the list
    # ------------------------------------------------------------------
    def _switch_section(self, key: str) -> None:
        self._cancel_capture()
        self._section = key
        self._rebuild()
        self.scroll.verticalScrollBar().setValue(0)

    def _rebuild(self) -> None:
        pos = self.scroll.verticalScrollBar().value()
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 8, 0)
        col.setSpacing(4)
        if self._section == "misc":
            note = QLabel("Esc always closes Settings, results and dialogs, whatever keys are set here. "
                          "In a text field, Enter keeps and Esc undoes what you typed.")
            note.setProperty("role", "muted")
            note.setWordWrap(True)
            col.addWidget(note)
            for sec, title in MISC_GROUPS:
                self._add_group(col, sec, title)
        else:
            self._add_group(col, self._section, None)
        col.addStretch(1)
        # The list is rebuilt after every change. A focused button that
        # disappears would leave this window with no focus, and Windows
        # then activates the main window, so keys stopped reaching it.
        # Keep the focus here, and the keyboard outline on the same button.
        nav = getattr(self, "_mmm_nav", None)
        ring_on = nav.target.objectName() if nav is not None and nav.current() is not None \
            and nav.target.objectName().startswith("remap|") else None
        old = self.scroll.takeWidget()
        if old is not None:
            fw = QApplication.focusWidget()
            if fw is not None and old.isAncestorOf(fw):
                self.setFocus(Qt.OtherFocusReason)
            old.hide()
            old.deleteLater()
        self.scroll.setWidget(host)
        self.scroll.verticalScrollBar().setValue(pos)
        if ring_on:
            again = host.findChild(QPushButton, ring_on)
            if again is not None:
                QTimer.singleShot(0, lambda b=again: nav.go(b) if ui_keys.usable(b) else None)
        changed = self.work != self.applied
        self.apply_btn.setEnabled(changed)

    def _add_group(self, col: QVBoxLayout, section: str, title: Optional[str]) -> None:
        if title:
            cap = QLabel(title.upper())
            cap.setProperty("role", "caps")
            col.addSpacing(8)
            col.addWidget(cap)
        major = ui_keys.MAJOR_ACTIONS.get(section, set())
        for action, keys in self.work[section].items():
            row = QHBoxLayout()
            row.setSpacing(6)
            name = QLabel(action_label(section, action))
            name.setMinimumWidth(270)
            row.addWidget(name)
            tag = QLabel("safeguarded" if action in major else "")
            tag.setProperty("role", "faint")
            tag.setFixedWidth(84)
            row.addWidget(tag)
            for i, k in enumerate(keys):
                capturing = self._capture == (section, action, i)
                b = make_button("Press a key…" if capturing else key_text(k), "accent" if capturing else "",
                                compact=True)
                b.setToolTip("Change this key")
                b.setObjectName(f"remap|{section}|{action}|key|{i}")
                b.setMinimumWidth(54)
                b.clicked.connect(lambda _c=False, s=section, a=action, j=i: self._start_capture(s, a, j))
                row.addWidget(b)
                x = make_button("×", "ghost", compact=True)
                x.setToolTip("Remove this key" if not (action in major and len(keys) == 1)
                             else "This key is safeguarded: it must keep at least one key")
                x.setObjectName(f"remap|{section}|{action}|x|{i}")
                x.setEnabled(not (action in major and len(keys) == 1))
                x.clicked.connect(lambda _c=False, s=section, a=action, j=i: self._remove_key(s, a, j))
                row.addWidget(x)
            adding = self._capture == (section, action, None)
            plus = make_button("Press a key…" if adding else "+", "accent" if adding else "", compact=True)
            plus.setToolTip("Add another key")
            plus.setObjectName(f"remap|{section}|{action}|plus")
            plus.clicked.connect(lambda _c=False, s=section, a=action: self._start_capture(s, a, None))
            row.addWidget(plus)
            row.addStretch(1)
            col.addLayout(row)

    # ------------------------------------------------------------------
    # Capturing a key
    # ------------------------------------------------------------------
    def _start_capture(self, section: str, action: str, index: Optional[int]) -> None:
        self._capture = (section, action, index)
        self.key_capturing = True
        self.status.setText(f"Press the new key for “{action_label(section, action)}”. "
                            "Every key counts now, Esc and Tab too; click Cancel to stop.")
        self.cancel_capture_btn.show()
        self._rebuild()

    def _cancel_capture(self) -> None:
        if not self.key_capturing:
            return
        self._end_capture()
        self.status.setText("")
        self._rebuild()

    def _end_capture(self) -> None:
        self._capture = None
        self.key_capturing = False
        self.cancel_capture_btn.hide()

    def key_capture(self, e) -> bool:
        """Called by ui_keys.KeyboardController for every key press while
        a key is being chosen. Always True (the key is used here)."""
        if e.isAutoRepeat():
            return True
        if e.key() == Qt.Key_Backtab:
            self.status.setText("Shift+Tab can't be used (it's kept for the key map). Press another key.")
            return True
        text = key_from_event(e)
        if not text:
            return True  # a lone Shift / Ctrl: wait for the real key
        self._assign(text)
        return True

    def _assign(self, text: str) -> None:
        section, action, index = self._capture
        self._end_capture()
        keys = self.work[section][action]
        old = keys[index] if index is not None and index < len(keys) else None
        if text == old or (index is None and text in keys):
            self.status.setText("No change.")
            self._rebuild()
            return
        other = next((b for b, ks in self.work[section].items()
                      if b != action and text in ks and not ui_keys.can_share(section, action, b)), None)
        if other is not None:
            okeys = self.work[section][other]
            other_name = action_label(section, other)
            if old is None and other in ui_keys.MAJOR_ACTIONS.get(section, ()) and len(okeys) == 1:
                ui_dialogs.warn(self, "Key already used",
                                f"“{key_text(text)}” is the only key for “{other_name}”, which must keep "
                                "a key. Give it another key first, or pick a different key here.")
                self.status.setText("")
                self._rebuild()
                return
            if old is not None:
                msg = (f"“{key_text(text)}” is already used for “{other_name}” on this screen. "
                       f"Swap them? “{other_name}” would get “{key_text(old)}”.")
                yes = "Swap"
            else:
                msg = (f"“{key_text(text)}” is already used for “{other_name}” on this screen. "
                       f"Move it here? “{other_name}” would lose it.")
                yes = "Move it here"
            if not ui_dialogs.ask_yes_no(self, "Key already used", msg, yes, "Cancel"):
                self.status.setText("Nothing changed.")
                self._rebuild()
                return
            j = okeys.index(text)
            if old is not None and old not in okeys:
                okeys[j] = old
            else:
                okeys.pop(j)
        if index is None or old is None:
            keys.append(text)
        else:
            keys[index] = text
        self.status.setText(f"“{action_label(section, action)}”: {', '.join(key_text(k) for k in keys)}. "
                            "Press Apply to use it.")
        self._rebuild()

    def _remove_key(self, section: str, action: str, index: int) -> None:
        self._cancel_capture()
        keys = self.work[section][action]
        if action in ui_keys.MAJOR_ACTIONS.get(section, ()) and len(keys) <= 1:
            return
        if 0 <= index < len(keys):
            keys.pop(index)
        self._rebuild()

    # ------------------------------------------------------------------
    # Apply / Reset / Close
    # ------------------------------------------------------------------
    def apply(self) -> bool:
        """Use the changed keys. True if they are now saved."""
        self._cancel_capture()
        old, new = self.applied, copy.deepcopy(self.work)
        if new == old:
            self.status.setText("Nothing to apply.")
            return False
        majors = ui_keys.changed_major(old, new)
        self.controller.set_keymap(ui_keys.KeyMap(new))
        if majors:
            names = [f"{action_label(s, a)} ({dict(SECTIONS + MISC_GROUPS).get(s, s)})" for s, a in majors]
            dlg = KeepKeysDialog(self, names)
            if dlg.exec() != dlg.Accepted:
                self.controller.set_keymap(ui_keys.KeyMap(old))
                self.work = copy.deepcopy(old)
                self.status.setText("The old keys are back. Nothing was saved.")
                self._rebuild()
                return False
        ui_settings.set_value("keymap", ui_keys.keymap_overrides(new))
        self.applied = new
        self.status.setText("Saved. The new keys are in use.")
        self._rebuild()
        return True

    def _reset(self) -> None:
        self._cancel_capture()
        if not ui_dialogs.ask_yes_no(self, "Reset to defaults",
                                     "Put every key back to its default? This applies at once.",
                                     "Reset", "Cancel"):
            return
        d = ui_keys.default_keymap()
        self.controller.set_keymap(ui_keys.KeyMap(d))
        ui_settings.set_value("keymap", {})
        self.applied = d
        self.work = copy.deepcopy(d)
        self.status.setText("Every key is back to its default.")
        self._rebuild()

    def reject(self) -> None:
        """Close / × / Esc. Changes that weren't applied are dropped, after
        asking."""
        if self.key_capturing:  # Esc while choosing a key is captured, so this is Close / ×
            self._end_capture()
        if self.work != self.applied and not ui_dialogs.ask_yes_no(
                self, "Changes not applied",
                "Some key changes haven't been applied. Close and forget them?",
                "Close and forget", "Keep editing"):
            self._rebuild()
            return
        super().reject()
