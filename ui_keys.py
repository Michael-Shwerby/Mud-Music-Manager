"""
Mud Music Manager - Keyboard control (v0.16.20, Checklist item 27)

The move-and-select framework shared by every screen:

- the keymap (screen -> action -> keys), kept as plain data so it can be
  made remappable later (item 27, part 5) without touching the screens;
- the "typing in a field" rule: while a text field has the focus no
  shortcut fires, and Enter accepts the typing and leaves the field;
- spatial movement (W/A/S/D and the arrow keys pick the nearest control
  in that direction);
- KeyRing, the highlight drawn around the control the keys are on;
- KeyboardController, an application-wide event filter that hands key
  presses to the screen that is showing.

Keys only work while the main window is focused (not system-wide), and
never while a dialog, a file picker, the CMD window or the Debug Window
is in front. Built so far: the Main Menu (part 1), the Edit page
(part 2, v0.16.23), Settings / results / dialogs (part 3, v0.16.24) and
the Tab key map (part 4, v0.16.25: KeyMapPanel, built from MAP_LAYOUT
and the keymap, so remapped keys show up in it). Remapping is part 5.

v0.16.29 (Checklist item 30): Main Menu actions expand_all (O) and
collapse_all (Shift+O) for the Expand all / Collapse all buttons.

v0.16.30 (Checklist item 31): Main Menu action search (Ctrl+F or /) puts
the typing cursor in the playlist search bar; Back (Esc) clears a search
when nothing else is open.

v0.16.31 (Checklist item 32): Edit page action search (Ctrl+F or /) for
the song search bar; Back (Esc) clears a search before leaving the page.

v0.17.0: the outline moves in the same frame as the control it is on
(bars sliding, lists gliding), and keyboard scrolling glides where the
list scrolls smoothly (ui_widgets.ensure_visible / scroll_area_by).
"""

import copy
from typing import Dict, List, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractScrollArea, QAbstractSpinBox, QApplication, QDialog, QFileDialog,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QScrollArea, QTextEdit,
    QVBoxLayout, QWidget,
)

import logger
import ui_settings
import ui_widgets
from ui_theme import palette

try:
    from shiboken6 import isValid as is_valid
except Exception:  # pragma: no cover
    def is_valid(_obj) -> bool:
        return True


# ---------------------------------------------------------------------------
# Default keymap (the keys on install)
# ---------------------------------------------------------------------------
# Key names are QKeySequence text ("W", "Up", "Space", "Ctrl+S"...). The
# arrow keys are listed beside W/A/S/D so they do the same thing. One key
# can belong to two actions of a screen when they never apply at the same
# time (1 = Sync Now normally, = playlist 1 while choosing playlists).

DEFAULT_KEYMAP: Dict[str, Dict[str, List[str]]] = {
    "main": {
        "up": ["W", "Up"],
        "down": ["S", "Down"],
        "left": ["A", "Left"],
        "right": ["D", "Right"],
        "move_up": ["Shift+W", "Shift+Up"],        # move the highlighted playlist one place
        "move_down": ["Shift+S", "Shift+Down"],
        "select": ["Space"],
        "start": ["Return", "Enter"],       # while choosing playlists
        "position": ["Return", "Enter"],    # otherwise: type the highlighted playlist's position
        "back": ["Esc"],
        "sync": ["1"],
        "scan": ["2"],
        "integrity": ["3"],
        **{f"playlist_{n}": [str(n)] for n in range(1, 10)},
        "toggle_all": ["F"],
        "remove": ["R"],
        "add": ["Q"],
        "edit": ["E"],
        "expand_all": ["O"],                # v0.16.29 (item 30)
        "collapse_all": ["Shift+O"],
        "search": ["Ctrl+F", "/"],          # v0.16.30 (item 31): type in the search bar
        "change_folder": ["C"],
        "settings": ["G"],
        "cancel": ["X"],
        "cmd": ["Z"],
        "update": ["V"],
        "pause": ["P"],
        "queue": ["L"],                     # v0.16.33 (item 34): show / hide the queue list (Sync Now too since v0.16.36)
        "keymap": ["Tab"],                  # show / hide the on-screen key map (part 4)
    },
    # Edit page (part 2): W/S choose songs; A/D seek; no highlight outline.
    "edit": {
        "up": ["W", "Up"],
        "down": ["S", "Down"],
        "seek_back": ["A", "Left"],
        "seek_forward": ["D", "Right"],
        "play_selected": ["Q"],
        "exception": ["E"],
        "import_file": ["I"],               # v0.16.40 (item 47): use your own audio file for the song
        "search": ["Ctrl+F", "/"],          # v0.16.31 (item 32): type in the song search bar
        "play_pause": ["Space"],
        "volume_down": ["C"],
        "volume_up": ["V"],
        "prev_track": ["R"],
        "next_track": ["T"],
        "icon": ["1"],
        "name": ["2"],
        "link": ["3"],
        "logs": ["F"],
        "settings": ["G"],
        "cmd": ["Z"],
        "cancel": ["X"],
        "save": ["Ctrl+S"],
        "back": ["Esc"],
        "keymap": ["Tab"],
    },
    # Settings and the Scan / Check Integrity results (part 3). Esc closes
    # the overlay on its own (OverlayHost), so it isn't listed.
    "overlay": {
        "up": ["W", "Up"],
        "down": ["S", "Down"],
        "left": ["A", "Left"],
        "right": ["D", "Right"],
        "select": ["Space"],
        "confirm": ["Return", "Enter"],
        "settings": ["G"],
        "keymap": ["Tab"],
    },
    # Dialogs (part 3). Esc cancels and Enter in a field confirms, as Qt
    # dialogs already do. In a text field W/S type, so Up / Down step from
    # the field to the buttons and back.
    "dialog": {
        "up": ["W", "Up"],
        "down": ["S", "Down"],
        "left": ["A", "Left"],
        "right": ["D", "Right"],
        "select": ["Space"],
        "confirm": ["Return", "Enter"],
        "field_up": ["Up"],
        "field_down": ["Down"],
        "keymap": ["Tab"],
    },
}

# ---------------------------------------------------------------------------
# Remapping (part 5)
# ---------------------------------------------------------------------------
# The keys whose misbinding would seriously get in the way (movement,
# selection, Back). They always keep at least one key, and changing them
# asks "Keep these keys?" with a countdown that puts the old keys back.

MAJOR_ACTIONS: Dict[str, Set[str]] = {
    "main": {"up", "down", "left", "right", "select", "back"},
    "edit": {"up", "down", "back"},
    "overlay": {"up", "down", "left", "right", "select"},
    "dialog": {"up", "down", "left", "right", "select", "field_up", "field_down"},
}

# Names shown in the remap window, in the keymap's order.
ACTION_LABELS: Dict[str, Dict[str, str]] = {
    "main": {
        "up": "Move up", "down": "Move down", "left": "Move left", "right": "Move right",
        "move_up": "Move the playlist up", "move_down": "Move the playlist down",
        "select": "Press / expand / tick", "start": "Start the run (while choosing)",
        "position": "Type the playlist's position", "back": "Back (leave Remove / choosing, clear the search)",
        "sync": "Sync Now", "scan": "Scan", "integrity": "Check Integrity",
        **{f"playlist_{n}": f"Tick playlist {n} (while choosing)" for n in range(1, 10)},
        "toggle_all": "Check all / Uncheck all", "remove": "Remove",
        "add": "Add (Queue Another Playlist while adding)", "edit": "Edit",
        "expand_all": "Expand all playlists", "collapse_all": "Collapse all playlists",
        "search": "Search playlists", "change_folder": "Change Music folder", "settings": "Settings", "cancel": "Cancel or close what's open",
        "cmd": "CMD", "update": "Check For Updates", "pause": "Pause / Resume a sync",
        "queue": "Show / hide the queue list (Add / Sync Now)",
        "keymap": "Show / hide the key map",
    },
    "edit": {
        "up": "Select the song above", "down": "Select the song below",
        "seek_back": "Skip 5 s back", "seek_forward": "Skip 5 s forward",
        "play_selected": "Play the selected song", "exception": "Exception for the selected song",
        "import_file": "Use your own file for the selected song",
        "search": "Search songs", "play_pause": "Play / pause", "volume_down": "Volume down", "volume_up": "Volume up",
        "prev_track": "Previous track", "next_track": "Next track",
        "icon": "Change the picture", "name": "Edit the name", "link": "Edit the link",
        "logs": "View Sync Logs", "settings": "Settings", "cmd": "CMD", "cancel": "Cancel the launch scan",
        "save": "Save", "back": "Back (clears the search first)", "keymap": "Show / hide the key map",
    },
    "overlay": {
        "up": "Move up", "down": "Move down", "left": "Move left", "right": "Move right",
        "select": "Select / press", "confirm": "Press the highlighted button",
        "settings": "Close Settings", "keymap": "Show / hide the key map",
    },
    "dialog": {
        "up": "Move up", "down": "Move down", "left": "Move left", "right": "Move right",
        "select": "Press the highlighted button", "confirm": "Press the highlighted button (2nd key)",
        "field_up": "Leave a text field upward", "field_down": "Leave a text field downward",
        "keymap": "Show / hide the key map",
    },
}

# Actions that are never live at the same moment may share a key on one
# screen (1 = Sync Now normally, = playlist 1 while choosing).
_MAIN_NORMAL_ONLY = {"sync", "scan", "integrity", "position"}
_MAIN_CHOOSING_ONLY = {f"playlist_{n}" for n in range(1, 10)} | {"start"}
_DIALOG_FIELD_ONLY = {"field_up", "field_down"}


def can_share(section: str, a: str, b: str) -> bool:
    if section == "main":
        return (a in _MAIN_NORMAL_ONLY and b in _MAIN_CHOOSING_ONLY) or \
               (b in _MAIN_NORMAL_ONLY and a in _MAIN_CHOOSING_ONLY)
    if section == "dialog":
        return (a in _DIALOG_FIELD_ONLY) != (b in _DIALOG_FIELD_ONLY)
    return False


def normalize_key(text: str) -> str:
    """One key in a fixed spelling ("ctrl+s" -> "Ctrl+S"); "" if it isn't
    a single valid key."""
    if not isinstance(text, str) or not text.strip():
        return ""
    seq = QKeySequence(text)
    if seq.isEmpty() or seq.count() != 1:
        return ""
    return seq.toString(QKeySequence.PortableText)


def _normalized(data: Dict[str, Dict[str, List[str]]]) -> Dict[str, Dict[str, List[str]]]:
    out = {}
    for s, acts in data.items():
        out[s] = {}
        for a, keys in acts.items():
            seen = []
            for k in keys:
                n = normalize_key(k)
                if n and n not in seen:
                    seen.append(n)
            out[s][a] = seen
    return out


def default_keymap() -> Dict[str, Dict[str, List[str]]]:
    return _normalized(DEFAULT_KEYMAP)


def load_keymap() -> Dict[str, Dict[str, List[str]]]:
    """The defaults with the keys saved in ui_settings.json ("keymap") on
    top. Unknown screens / actions and invalid keys are ignored, and a
    major action never ends up without a key."""
    data = default_keymap()
    saved = ui_settings.get("keymap")
    if not isinstance(saved, dict):
        return data
    try:
        for s, acts in saved.items():
            if s not in data or not isinstance(acts, dict):
                continue
            for a, keys in acts.items():
                if a not in data[s] or not isinstance(keys, list):
                    continue
                clean = []
                for k in keys:
                    n = normalize_key(k)
                    if n and n not in clean:
                        clean.append(n)
                if not clean and a in MAJOR_ACTIONS.get(s, ()):
                    continue
                data[s][a] = clean
    except Exception as e:  # a hand-edited file must never stop the app
        logger.log_error(f"Keyboard: ignoring the saved key changes ({e})")
        return default_keymap()
    return data


def keymap_overrides(data: Dict[str, Dict[str, List[str]]]) -> Dict[str, Dict[str, List[str]]]:
    """Only what differs from the defaults (what ui_settings.json stores)."""
    base = default_keymap()
    out = {}
    for s, acts in data.items():
        diff = {a: list(k) for a, k in acts.items() if a in base.get(s, {}) and k != base[s][a]}
        if diff:
            out[s] = diff
    return out


def changed_major(old: Dict[str, Dict[str, List[str]]], new: Dict[str, Dict[str, List[str]]]) -> List[Tuple[str, str]]:
    return [(s, a) for s, acts in MAJOR_ACTIONS.items() for a in sorted(acts)
            if old.get(s, {}).get(a) != new.get(s, {}).get(a)]


# ---------------------------------------------------------------------------
# On-screen key map (Tab, part 4)
# ---------------------------------------------------------------------------
# What the map lists for each keymap section: (heading, [(label, items)]).
# Items are action names (all their keys), "1:<action>" (its first key
# only), "key:<name>" (a fixed key, e.g. Esc) or "txt:<word>" (plain
# text). Keys are read from the keymap, so a remapped key shows up here.

MOVE_ITEMS = ["up", "left", "down", "right"]

MAP_LAYOUT = {
    "main": [
        ("Move", [
            ("Move around", MOVE_ITEMS),
            ("Press a button / expand a playlist", ["select"]),
            ("Move the playlist up / down", ["move_up", "move_down"]),
            ("Type the playlist's position", ["position"]),
        ]),
        ("Actions", [
            ("Sync Now / Scan / Check Integrity", ["sync", "scan", "integrity"]),
            ("Add / Remove / Edit", ["add", "remove", "edit"]),
            ("Expand all / Collapse all", ["expand_all", "collapse_all"]),
            ("Search playlists", ["search"]),
            ("Change Music folder", ["change_folder"]),
            ("Check For Updates", ["update"]),
            ("Settings / CMD", ["settings", "cmd"]),
            ("Cancel or close what's open", ["cancel"]),
            ("Leave Remove / choosing, clear the search", ["back"]),
        ]),
        ("While searching", [
            ("Keep the search / undo the typing", ["key:Enter", "key:Esc"]),
        ]),
        ("While choosing playlists", [
            ("Tick a playlist by number", [f"playlist_{n}" for n in range(1, 10)]),
            ("Tick the highlighted playlist", ["select"]),
            ("Tick a run of playlists", ["txt:hold", "1:select", "txt:+", "1:up", "1:down"]),
            ("Check all / Uncheck all", ["toggle_all"]),
            ("Start", ["start"]),
        ]),
        ("While syncing", [
            ("Pause / Resume", ["pause"]),
            ("Show / hide the queue list", ["queue"]),
        ]),
        ("Help", [
            ("Show / hide this map", ["keymap"]),
        ]),
    ],
    "edit": [
        ("Songs", [
            ("Select the song above / below", ["up", "down"]),
            ("Play the selected song", ["play_selected"]),
            ("Exception for the selected song", ["exception"]),
            ("Use your own file for the selected song", ["import_file"]),
            ("Search songs", ["search"]),
        ]),
        ("Player", [
            ("Play / pause", ["play_pause"]),
            ("Skip 5 s back / forward", ["seek_back", "seek_forward"]),
            ("Volume down / up", ["volume_down", "volume_up"]),
            ("Previous / next track", ["prev_track", "next_track"]),
        ]),
        ("Playlist", [
            ("Change picture / name / link", ["icon", "name", "link"]),
            ("View Sync Logs", ["logs"]),
            ("Save", ["save"]),
            ("Back (clear the search first)", ["back"]),
        ]),
        ("Other", [
            ("Settings / CMD", ["settings", "cmd"]),
            ("Cancel the launch scan", ["cancel"]),
            ("While typing: keep / undo the text", ["key:Enter", "key:Esc"]),
            ("Show / hide this map", ["keymap"]),
        ]),
    ],
    "overlay": [
        ("Move", [
            ("Move around", MOVE_ITEMS),
            ("Select / press", ["select"]),
            ("Press the highlighted button", ["confirm"]),
        ]),
        ("Close", [
            ("Close", ["key:Esc"]),
            ("Close Settings", ["settings"]),
            ("Close results (nothing highlighted)", ["1:select"]),
            ("Show / hide this map", ["keymap"]),
        ]),
    ],
    "dialog": [
        ("Buttons", [
            ("Move between buttons", MOVE_ITEMS),
            ("Press the highlighted button", ["select", "confirm"]),
        ]),
        ("Text fields", [
            ("Leave the field / go back to it", ["field_up", "field_down"]),
            ("Confirm", ["key:Enter"]),
        ]),
        ("Close", [
            ("Cancel", ["key:Esc"]),
            ("Show / hide this map", ["keymap"]),
        ]),
    ],
}

_KEY_SYMBOLS = {"Up": "↑", "Down": "↓", "Left": "←", "Right": "→", "Return": "Enter", "Escape": "Esc"}


def key_label(text: str) -> str:
    """How a key is written on the map ("Up" -> "↑", "Return" -> "Enter")."""
    return "+".join(_KEY_SYMBOLS.get(part, part) for part in text.split("+"))


# Movement actions repeat while the key is held; every other action fires
# once per press.
MOVE_ACTIONS = ("up", "down", "left", "right")
REPEAT_ACTIONS = MOVE_ACTIONS + ("move_up", "move_down", "seek_back", "seek_forward",
                                 "volume_down", "volume_up")

_MOD_MASK = (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier).value
_SHIFT = Qt.ShiftModifier.value
_MODIFIER_KEYS = {k.value for k in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_AltGr, Qt.Key_Meta, Qt.Key_CapsLock)}


def _int(v) -> int:
    return int(getattr(v, "value", v))


class KeyMap:
    """Looks up which actions a key press means on a screen."""

    def __init__(self, keymap: Optional[Dict[str, Dict[str, List[str]]]] = None):
        self._map = keymap or DEFAULT_KEYMAP
        self._parsed: Dict[str, List[Tuple[int, int, str]]] = {}
        for screen, actions in self._map.items():
            entries = []
            for action, keys in actions.items():
                for text in keys:
                    seq = QKeySequence(text)
                    if seq.isEmpty():
                        continue
                    comb = seq[0]
                    entries.append((_int(comb.key()), _int(comb.keyboardModifiers()) & (_MOD_MASK | _SHIFT), action))
            self._parsed[screen] = entries

    def actions(self, e, screen: str) -> Set[str]:
        """Every action of `screen` bound to this key. The keypad flag is
        ignored (the number pad works). Shift counts only where a binding
        names it (Shift+W); otherwise it is ignored, so Shift+A still moves
        like A. A Shift binding wins over the plain key."""
        key = _int(e.key())
        raw = _int(e.modifiers())
        mods = raw & _MOD_MASK
        entries = [(k, m, a) for k, m, a in self._parsed.get(screen, ()) if k == key]
        if raw & _SHIFT:
            shifted = {a for k, m, a in entries if m == mods | _SHIFT}
            if shifted:
                return shifted
        return {a for k, m, a in entries if m == mods}

    def keys_for(self, screen: str, action: str) -> List[str]:
        return list(self._map.get(screen, {}).get(action, []))

    @property
    def data(self) -> Dict[str, Dict[str, List[str]]]:
        """A copy of the whole keymap (screen -> action -> keys)."""
        return copy.deepcopy(self._map)

    def map_row(self, screen: str, items: List[str]) -> List[tuple]:
        """One row of the key map as [("key", text) | ("txt", text)]. The
        actions' first keys come first, then their second keys (W A S D,
        then ↑ ← ↓ →); repeats are dropped; the number keys 1-9 become 1–9."""
        out: List[tuple] = []
        lists = []
        for it in items:
            if it.startswith("key:"):
                lists.append([("key", it[4:])])
            elif it.startswith("txt:"):
                lists.append([("txt", it[4:])])
            else:
                first_only = it.startswith("1:")
                keys = self.keys_for(screen, it[2:] if first_only else it)
                lists.append([("key", key_label(k)) for k in (keys[:1] if first_only else keys)])
        has_txt = any(kind == "txt" for lst in lists for kind, _ in lst)
        if has_txt:  # a phrase ("hold Space + W S"): keep the order as written
            seq = [x for lst in lists for x in lst]
        else:
            seq = [lst[i] for i in range(max((len(l) for l in lists), default=0)) for lst in lists if i < len(lst)]
        for x in seq:
            if x[0] == "txt" or x not in out:
                out.append(x)
        digits = [t for k, t in out if k == "key"]
        if digits == [str(n) for n in range(1, 10)]:
            out = [("key", "1–9")]
        return out


# ---------------------------------------------------------------------------
# Helpers for the screens
# ---------------------------------------------------------------------------

def is_typing_widget(w: Optional[QWidget]) -> bool:
    """A field the keys type into: no shortcut fires while it has focus."""
    if w is None:
        return False
    if isinstance(w, QLineEdit):
        return not w.isReadOnly()
    if isinstance(w, (QTextEdit, QPlainTextEdit)):
        return not w.isReadOnly()
    return isinstance(w, QAbstractSpinBox)


def usable(w: Optional[QWidget]) -> bool:
    return w is not None and is_valid(w) and w.isVisible() and w.isEnabled()


def press(button) -> bool:
    """Click a button the way the mouse would: only if it is showing and
    enabled (a hidden button's click() would still run otherwise)."""
    if usable(button):
        button.click()
        return True
    return False


def rect_in(w: QWidget, top: QWidget) -> QRect:
    return QRect(w.mapTo(top, QPoint(0, 0)), w.size())


def on_screen(w: QWidget) -> bool:
    """Showing and not scrolled out of view."""
    return is_valid(w) and w.isVisible() and not w.visibleRegion().isEmpty()


def spatial_pick(origin: QRect, candidates: Sequence[Tuple[object, QRect]], direction: str):
    """The candidate nearest `origin` in `direction` ('up', 'down', 'left',
    'right'). Candidates lined up with the origin (overlapping it across
    the direction of travel) win over ones off to the side."""
    oc = origin.center()
    best, best_score = None, None
    for item, r in candidates:
        c = r.center()
        if direction in ("up", "down"):
            major = (oc.y() - c.y()) if direction == "up" else (c.y() - oc.y())
            minor = abs(c.x() - oc.x())
            in_line = r.left() < origin.right() and r.right() > origin.left()
        else:
            major = (oc.x() - c.x()) if direction == "left" else (c.x() - oc.x())
            minor = abs(c.y() - oc.y())
            in_line = r.top() < origin.bottom() and r.bottom() > origin.top()
        if major <= 0:
            continue
        score = (0, major + minor * 0.3) if in_line else (1, major + minor * 2)
        if best_score is None or score < best_score:
            best, best_score = item, score
    return best


# ---------------------------------------------------------------------------
# Highlight ring
# ---------------------------------------------------------------------------

class KeyRing(QWidget):
    """The accent-coloured outline around the control the keys are on.
    A transparent child of the window drawn on top of the target, so no
    widget's own look or size changes. Mouse clicks pass through it. It
    follows the target as the page scrolls, rows expand or the window is
    resized, and hides itself when the target goes away."""

    MARGIN = 3
    PEN = 2.0

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._target: Optional[QWidget] = None
        self._owner = None  # who placed it (a page or a ContainerNav)
        self._radius = 12.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._follow)
        # v0.17.0: the target and the widgets holding it are watched, so the
        # outline moves in the same frame as they do (a bar sliding, a list
        # gliding). The 40 ms timer alone left it a frame or two behind, so
        # it tore away from the bar mid-animation; it stays as a backstop.
        self._watched: List[QWidget] = []
        # v0.16.35 (Checklist item 36): what it was on when the last mouse
        # button went down, before that press hid it
        self._press_ts = None
        self._press_target: Optional[QWidget] = None
        self._press_owner = None
        self.hide()

    def note_press(self, e) -> None:
        """A mouse button went down in this window; remember what the ring
        was following, since the press is about to hide it. The press
        reaches the event filter once per widget it passes through on its
        way up, so only the first one (by timestamp) is recorded."""
        ts = e.timestamp()
        if ts == self._press_ts:
            return
        self._press_ts = ts
        shown = self.is_shown()
        self._press_target = self.target if shown else None
        self._press_owner = self._owner if shown else None

    def target_at_press(self, owner=None) -> Optional[QWidget]:
        """What the ring was following when the last mouse button went down
        (None if it was hidden). With `owner`, only if that owner placed it."""
        t = self._press_target
        if t is None or not is_valid(t) or (owner is not None and self._press_owner is not owner):
            return None
        return t

    @property
    def target(self) -> Optional[QWidget]:
        t = self._target
        return t if t is not None and is_valid(t) else None

    def is_shown(self, owner=None) -> bool:
        """Following a target. True even while it is momentarily not drawn
        (target just rebuilt and not laid out yet, or scrolled out of view);
        False once hidden by the mouse or the target went away. With
        `owner`, only if that owner placed it (the Main Menu and Settings
        share the window's ring)."""
        return self._timer.isActive() and (owner is None or self._owner is owner)

    def show_for(self, target: QWidget, radius: float = 9.0, owner=None) -> None:
        self._target = target
        self._owner = owner
        self._radius = radius + self.MARGIN
        self._watch(target)
        self.show()
        self.raise_()
        self._follow()
        self._timer.start()

    def hide_ring(self) -> None:
        self._timer.stop()
        self._watch(None)
        self.hide()

    def _watch(self, target: Optional[QWidget]) -> None:
        """Watch `target` and its ancestors up to this ring's parent for
        moves / resizes (None: stop watching)."""
        for w in self._watched:
            try:
                w.removeEventFilter(self)
            except RuntimeError:  # already deleted
                pass
        self._watched = []
        parent = self.parentWidget()
        w = target
        while w is not None and w is not parent:
            w.installEventFilter(self)
            self._watched.append(w)
            w = w.parentWidget()

    def eventFilter(self, obj, e):
        if e.type() in (QEvent.Move, QEvent.Resize) and self._timer.isActive():
            self._follow()
        return False

    def _follow(self) -> None:
        t = self.target
        parent = self.parentWidget()
        if t is None or parent is None or not t.isVisible() or t.window() is not parent.window():
            self.hide_ring()
            return
        m = self.MARGIN + 1
        r = rect_in(t, parent).adjusted(-m, -m, m, m)
        # never draw over the controls around a scrolled list
        clip = t.visibleRegion().boundingRect()
        if clip.isEmpty():
            self.hide()
            return
        clip = QRect(t.mapTo(parent, clip.topLeft()), clip.size()).adjusted(-m, -m, m, m)
        r = r.intersected(clip)
        if r != self.geometry():
            self.setGeometry(r)
        if not self.isVisible():
            self.show()
        self.update()

    def paintEvent(self, _event):
        t = self.target
        if t is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m = self.MARGIN + 1
        full = QRectF(rect_in(t, self.parentWidget()).adjusted(-m, -m, m, m)).translated(-self.x(), -self.y())
        half = self.PEN / 2 + 0.5
        p.setPen(QPen(QColor(palette().accent), self.PEN))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(full.adjusted(half, half, -half, -half), self._radius, self._radius)


# ---------------------------------------------------------------------------
# Buttons of one container: Settings, results, dialogs (part 3)
# ---------------------------------------------------------------------------

def _scroll_area_of(w: QWidget, container: QWidget) -> Optional[QAbstractScrollArea]:
    p = w.parentWidget()
    while p is not None and p is not container:
        if isinstance(p, QAbstractScrollArea):
            return p
        p = p.parentWidget()
    return None


class ContainerNav:
    """Move-and-select among the buttons inside one container (the
    Settings page, the results page, a dialog) and, for dialogs, its text
    fields too: landing on a field puts the typing cursor in it. When no
    control is on screen in the W/S direction, the container's list
    scrolls one step instead. `default` gives the control the first key
    lands on (e.g. Cancel in a dialog)."""

    RADIUS = 10
    SCROLL_PX = 60

    def __init__(self, container: QWidget, ring: KeyRing, default=None, fields: bool = False):
        self.container = container
        self.ring = ring
        self._default = default
        self.fields = fields
        self.target: Optional[QWidget] = None

    def buttons(self) -> List[QWidget]:
        return [b for b in self.container.findChildren(QAbstractButton) if usable(b) and on_screen(b)]

    def _candidates(self) -> List[QWidget]:
        out = self.buttons()
        if self.fields:
            out += [f for f in self.container.findChildren(QLineEdit)
                    if usable(f) and not f.isReadOnly() and on_screen(f)]
        return out

    def default(self) -> Optional[QWidget]:
        w = self._default() if self._default else None
        if usable(w) and on_screen(w):
            return w
        cands = sorted(self.buttons(), key=lambda b: (rect_in(b, self.container).top(), rect_in(b, self.container).left()))
        return cands[0] if cands else None

    def current(self) -> Optional[QWidget]:
        t = self.target
        if t is None or not usable(t) or not self.ring.is_shown(self) or self.ring.target is not t:
            return None
        return t

    def wake(self) -> bool:
        """The first key only shows the outline (on the last control, else
        the default). True if it was hidden."""
        if self.current() is not None:
            return False
        t = self.target if (usable(self.target) and on_screen(self.target)
                            and isinstance(self.target, QAbstractButton)) else self.default()
        if t is not None:
            self.go(t)
        return True

    def go(self, w: QWidget) -> None:
        self.target = w
        if isinstance(w, QLineEdit):
            if self.ring.is_shown(self):
                self.ring.hide_ring()
            w.setFocus(Qt.TabFocusReason)
            w.end(False)
            return
        area = _scroll_area_of(w, self.container)
        if isinstance(area, QScrollArea):
            ui_widgets.ensure_visible(area, w, 10, 10)  # v0.17.0: glides where the area scrolls smoothly
        self.ring.show_for(w, self.RADIUS, owner=self)

    def move(self, direction: str, origin: Optional[QWidget] = None) -> None:
        """origin: a text field being left with Up / Down (dialogs)."""
        if origin is None:
            if self.wake():
                return
            origin = self.current()
        top = self.container
        cands = [(w, rect_in(w, top)) for w in self._candidates() if w is not origin]
        pick = spatial_pick(rect_in(origin, top), cands, direction)
        if pick is not None and isinstance(origin, QLineEdit) and isinstance(pick, QAbstractButton):
            # leaving a field onto the button row: the safe button first
            safe = self.default()
            if safe is not None and safe is not pick:
                r, s = rect_in(pick, top), rect_in(safe, top)
                if s.top() < r.bottom() and s.bottom() > r.top():
                    pick = safe
        if pick is not None:
            self.go(pick)
        elif direction in ("up", "down"):
            self._scroll(origin, -1 if direction == "up" else 1)

    def _scroll(self, origin: QWidget, sign: int) -> bool:
        area = _scroll_area_of(origin, self.container)
        if area is None or area.verticalScrollBar().maximum() <= 0:
            area = next((a for a in self.container.findChildren(QAbstractScrollArea)
                         if a.isVisible() and a.verticalScrollBar().maximum() > 0), None)
        if area is None:
            return False
        sb = area.verticalScrollBar()
        step = self.SCROLL_PX if isinstance(area, QScrollArea) else max(3, sb.singleStep() * 3)
        return ui_widgets.scroll_area_by(area, sign * step)  # v0.17.0: glides where it can


def _dialog_safe_button(dlg: QWidget) -> Optional[QWidget]:
    """The button a dialog's keys start on: the one marked kbd_first
    (Cancel, Keep editing, Not now, No...) so a stray Space can't delete
    or change anything. Single-button popups have only OK / Close."""
    for b in dlg.findChildren(QAbstractButton):
        if b.property("kbd_first") and usable(b):
            return b
    if isinstance(dlg, QMessageBox):
        b = dlg.escapeButton()
        if usable(b):
            return b
    return None


# ---------------------------------------------------------------------------
# The on-screen key map (Tab, part 4)
# ---------------------------------------------------------------------------

class KeyMapPanel(QWidget):
    """The keys of the current screen, shown on Tab. A small floating
    panel centred over the window in front (the main window or a dialog),
    never taking the keyboard focus, so it also fits over small dialogs.
    Built fresh from the keymap each time it opens."""

    RADIUS = 16

    def __init__(self):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(26, 20, 26, 22)
        self._body: Optional[QWidget] = None
        self.over: Optional[QWidget] = None   # the window it is shown over
        self.section = ""

    def show_map(self, keymap: KeyMap, section: str, title: str, over: QWidget) -> None:
        if self._body is not None:
            # hidden at once: inside a dialog's own event loop the
            # deleteLater() only happens once the dialog closes
            self._body.hide()
            self._outer.removeWidget(self._body)
            self._body.deleteLater()
        self._body = self._build(keymap, section, title)
        self._outer.addWidget(self._body)
        self.section, self.over = section, over
        self.adjustSize()
        frame = over.frameGeometry()
        geo = self.frameGeometry()
        geo.moveCenter(frame.center())
        screen = over.screen().availableGeometry() if over.screen() else None
        if screen is not None:  # keep it on screen
            geo.moveLeft(max(screen.left(), min(geo.left(), screen.right() - geo.width())))
            geo.moveTop(max(screen.top(), min(geo.top(), screen.bottom() - geo.height())))
        self.move(geo.topLeft())
        self.show()
        self.raise_()

    def _build(self, keymap: KeyMap, section: str, title: str) -> QWidget:
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        head = QHBoxLayout()
        t = QLabel(f"Keyboard — {title}")
        f = t.font()
        f.setPointSizeF(13.5)
        f.setBold(True)
        t.setFont(f)
        head.addWidget(t)
        head.addStretch(1)
        hint = QLabel(f"{' or '.join(key_label(k) for k in keymap.keys_for(section, 'keymap')) or 'Tab'} or Esc closes")
        hint.setProperty("role", "muted")
        head.addWidget(hint)
        lay.addLayout(head)

        sections = MAP_LAYOUT.get(section, [])
        n_cols = 2 if len(sections) > 3 else 1
        cols = QHBoxLayout()
        cols.setSpacing(34)
        total = sum(len(rows) + 1 for _, rows in sections)
        col_boxes = [QVBoxLayout() for _ in range(n_cols)]
        for box in col_boxes:
            box.setSpacing(6)
        filled, ci = 0, 0
        for heading, rows in sections:
            if ci < n_cols - 1 and filled >= total / n_cols:
                ci += 1
            box = col_boxes[ci]
            cap = QLabel(heading.upper())
            cap.setProperty("role", "caps")
            box.addWidget(cap)
            grid = QGridLayout()
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(6)
            for r, (label, items) in enumerate(rows):
                keys = QHBoxLayout()
                keys.setSpacing(4)
                for kind, text in keymap.map_row(section, items):
                    w = QLabel(text)
                    w.setProperty("role", "keycap" if kind == "key" else "muted")
                    keys.addWidget(w)
                keys.addStretch(1)
                grid.addLayout(keys, r, 0)
                desc = QLabel(label)
                grid.addWidget(desc, r, 1)
            grid.setColumnStretch(1, 1)
            box.addLayout(grid)
            box.addSpacing(8)
            filled += len(rows) + 1
        for box in col_boxes:
            box.addStretch(1)
            cols.addLayout(box)
        lay.addLayout(cols)
        return body

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = palette()
        p.setPen(QPen(QColor(pal.accent), 1.5))
        p.setBrush(QColor(pal.panel_bg))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), self.RADIUS, self.RADIUS)


# ---------------------------------------------------------------------------
# Application-wide key handling
# ---------------------------------------------------------------------------

class KeyboardController(QObject):
    """Installed on the QApplication. Decides whether a key press is a
    shortcut at all, then hands it to the screen that is showing:

        screen = window.key_screen()   # a page with key_pressed(), or None
        screen.key_pressed(actions, event) -> bool (True = used)
        screen.key_released(actions, event)
        screen.key_reset()             # e.g. the window lost focus
        window.key_overlay(actions)    # overlay open: G closes Settings
        page.key_default()             # overlay page: first control (optional)
        page.KEY_SPACE_CLOSES          # overlay page: Space with nothing
                                       # highlighted closes it (results)

    Keys reach the main window only while it is the active window with
    no dialog open; while one of the app's dialogs is open (v0.16.24),
    they reach that dialog instead. File pickers, the CMD window and the
    Debug Window are never touched."""

    def __init__(self, window, keymap: Optional[KeyMap] = None):
        super().__init__(window)
        self.window = window
        self.keymap = keymap or KeyMap(load_keymap())  # with the saved remaps (part 5)
        self.ring = KeyRing(window.centralWidget())
        self._overlay_navs = {}
        self.map_panel = KeyMapPanel()
        QApplication.instance().installEventFilter(self)

    # -- gating -----------------------------------------------------------
    @staticmethod
    def _dialog() -> Optional[QDialog]:
        """The app dialog the keys go to, if one is open and in front."""
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QDialog) and not isinstance(modal, QFileDialog) and modal.isActiveWindow():
            return modal
        return None

    def _ours(self, obj) -> bool:
        """Only the first delivery of a key event (to the focused widget,
        or to the window when nothing has focus) is looked at; the copies
        Qt passes up to the parents are ignored."""
        if not isinstance(obj, QWidget):
            return False
        modal = QApplication.activeModalWidget()
        if modal is not None:
            win = self._dialog()
            if win is None:
                return False
        else:
            win = self.window
            if not win.isActiveWindow():
                return False
        if obj.window() is not win:
            return False
        fw = QApplication.focusWidget()
        return obj is (fw if fw is not None else win)

    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.KeyPress:
            if self._ours(obj):
                dlg = self._dialog()
                if dlg is not None and getattr(dlg, "key_capturing", False):
                    return bool(dlg.key_capture(e))  # the remap window is waiting for a key
                if self._map_key(e):
                    return True
                used = self._dialog_key(dlg, e) if dlg is not None else self._key_press(e)
                # Tab never moves between controls, even when it isn't bound (part 5)
                return used or e.key() in (Qt.Key_Tab, Qt.Key_Backtab)
        elif t == QEvent.KeyRelease:
            if self._ours(obj) and not e.isAutoRepeat() and self._dialog() is None:
                screen = self.window.key_screen()
                if screen is not None:
                    screen.key_released(self.keymap.actions(e, screen.KEY_SCREEN), e)
        elif t in (QEvent.WindowDeactivate, QEvent.Hide) and obj is self.map_panel.over \
                and self.map_panel.isVisible():
            self.map_panel.hide()  # its window went behind another or closed
            if t == QEvent.WindowDeactivate and obj is self.window:
                screen = self.window.key_screen()
                if screen is not None:
                    screen.key_reset()
        elif t == QEvent.MouseButtonPress:
            if self.map_panel.isVisible():
                self.map_panel.hide()
            # The ring is for the keys; using the mouse hides it until the
            # next key press (it comes back on the same control).
            if isinstance(obj, QWidget):
                if obj.window() is self.window:
                    self.ring.note_press(e)
                    if self.ring.isVisible():
                        self.ring.hide_ring()
                else:
                    nav = getattr(obj.window(), "_mmm_nav", None)
                    if nav is not None and nav.ring.isVisible():
                        nav.ring.hide_ring()
        elif t == QEvent.WindowDeactivate and obj is self.window:
            screen = self.window.key_screen()
            if screen is not None:
                screen.key_reset()
        return False

    def _key_press(self, e) -> bool:
        fw = QApplication.focusWidget()
        win = self.window
        screen = win.key_screen()
        if is_typing_widget(fw):
            # Typing: the keys type as normal. Enter accepts the text (the
            # field's own handling runs first) and then leaves the field;
            # Esc lets the screen put back what was there and leave it.
            if screen is not None and screen.isAncestorOf(fw):
                if e.key() in (Qt.Key_Return, Qt.Key_Enter):
                    QTimer.singleShot(0, self._leave_field)
                elif e.key() == Qt.Key_Escape and hasattr(screen, "key_field_escape"):
                    return bool(screen.key_field_escape(fw))
            return False
        if win.key_overlay_open():
            return self._overlay_key(e)
        actions = self.keymap.actions(e, screen.KEY_SCREEN if screen is not None else "main")
        if screen is None or not actions:
            return False
        if e.isAutoRepeat() and not (actions & set(REPEAT_ACTIONS)):
            return True  # held down: fire once
        return bool(screen.key_pressed(actions, e))

    def set_keymap(self, keymap: KeyMap) -> None:
        """Use a new keymap at once (remap window, part 5)."""
        self.keymap = keymap
        if self.map_panel.isVisible():
            self.map_panel.hide()

    # -- the on-screen key map (part 4) ----------------------------------------
    def _map_context(self):
        """(keymap section, title, window) for wherever the keys go now."""
        dlg = self._dialog()
        if dlg is not None:
            return "dialog", dlg.windowTitle() or "Dialog", dlg
        win = self.window
        if win.key_overlay_open():
            page = win.overlay.current_page()
            title = "Settings" if page is getattr(win, "settings_page", None) else "Results"
            return "overlay", title, win
        screen = win.key_screen()
        if screen is not None and screen.KEY_SCREEN == "edit":
            return "edit", "Edit page", win
        return "main", "Main Menu", win

    def _map_key(self, e) -> bool:
        """Tab (and Shift+Tab, which used to move backwards between
        controls) shows / hides the map; neither moves between controls
        any more. While the map is open, Esc only closes it, and any other
        key closes it and then does its usual job. True = key used."""
        if _int(e.key()) in _MODIFIER_KEYS:
            return False  # Shift on its own (on the way to Shift+Tab, Shift+W) leaves the map open
        section, title, over = self._map_context()
        is_map_key = "keymap" in self.keymap.actions(e, section) or e.key() == Qt.Key_Backtab
        if self.map_panel.isVisible():
            if is_map_key or e.key() == Qt.Key_Escape:
                if not e.isAutoRepeat():
                    self.map_panel.hide()
                return True
            self.map_panel.hide()
            return False
        if is_map_key:
            if not e.isAutoRepeat():
                self.map_panel.show_map(self.keymap, section, title, over)
            return True
        return False

    # -- Settings / results in the overlay (part 3) --------------------------
    def _overlay_key(self, e) -> bool:
        win = self.window
        actions = self.keymap.actions(e, "overlay")
        if win.key_overlay(actions, e):  # G closes Settings
            return True
        page = win.overlay.current_page()
        if page is None or not actions:
            return False
        nav = self._overlay_navs.get(id(page))
        if nav is None or nav.container is not page:
            nav = ContainerNav(page, self.ring, default=getattr(page, "key_default", None))
            self._overlay_navs[id(page)] = nav
        return self._nav_key(nav, actions, e, space_closes=getattr(page, "KEY_SPACE_CLOSES", False),
                             close=getattr(page, "close_btn", None))

    # -- dialogs (part 3) ------------------------------------------------------
    def _dialog_key(self, dlg: QDialog, e) -> bool:
        nav = getattr(dlg, "_mmm_nav", None)
        if nav is None:
            nav = ContainerNav(dlg, KeyRing(dlg), default=lambda d=dlg: _dialog_safe_button(d), fields=True)
            dlg._mmm_nav = nav
        actions = self.keymap.actions(e, "dialog")
        fw = QApplication.focusWidget()
        if is_typing_widget(fw):
            # Typing: Enter confirms and Esc cancels as before. Up / Down
            # (which type nothing in a one-line field) step to the next
            # field or the buttons.
            if isinstance(fw, QLineEdit) and actions & {"field_up", "field_down"}:
                nav.move("up" if "field_up" in actions else "down", origin=fw)
                if nav.current() is not None:  # now on a button: leave the field
                    dlg.setFocus(Qt.OtherFocusReason)
                return True
            return False
        if not actions:
            return False
        # one button (OK / Close): Space with nothing highlighted presses it
        single = len(nav.buttons()) == 1
        return self._nav_key(nav, actions, e, space_closes=single, close=nav.buttons()[0] if single else None)

    def _nav_key(self, nav: ContainerNav, actions: set, e, space_closes: bool, close) -> bool:
        if e.isAutoRepeat() and not (actions & set(MOVE_ACTIONS)):
            return True
        for d in MOVE_ACTIONS:
            if d in actions:
                nav.move(d)
                return True
        cur = nav.current()
        if "select" in actions:
            if cur is not None:
                press(cur)
            elif space_closes and close is not None:
                press(close)
            else:
                nav.wake()
            return True
        if "confirm" in actions and cur is not None:
            press(cur)  # Enter presses the highlighted button, not the default one
            return True
        return False

    def _leave_field(self) -> None:
        fw = QApplication.focusWidget()
        screen = self.window.key_screen()
        if screen is not None and is_typing_widget(fw) and screen.isAncestorOf(fw):
            screen.setFocus(Qt.OtherFocusReason)
