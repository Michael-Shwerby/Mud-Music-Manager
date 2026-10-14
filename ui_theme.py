"""
Mud Music Manager - UI Theme (v0.16.0)

Colors, fonts and the application-wide Qt stylesheet. The dark palette
is sampled from Michael's "Main Menu Screen Appearance Image Example"
(near-black navy background with a teal glow top-left and an indigo
glow bottom-right, slightly lighter cards, cyan-to-indigo primary
button, green/amber status pills). The light palette mirrors it for the
Settings page's light-mode toggle.

Custom-painted widgets (ui_widgets.py) read colors via palette(), and
listen to bus.changed to repaint when the theme is switched.
"""

from dataclasses import dataclass
from string import Template

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QFont

import ui_settings

FONT_FAMILIES = ["Inter", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Arial", "Sans Serif"]
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono", "Monospace"]


@dataclass(frozen=True)
class Palette:
    name: str
    base: str            # window background
    glow1: str           # teal glow (top-left)
    glow2: str           # indigo glow (bottom-right)
    text: str
    muted: str
    faint: str
    accent: str          # cyan
    accent2: str         # indigo
    on_accent: str       # text on the gradient primary button
    panel_bg: str
    panel_border: str
    row_bg1: str
    row_bg2: str
    row_border: str
    row_exp1: str
    row_exp2: str
    row_exp_border: str
    btn_bg: str
    btn_border: str
    btn_hover: str
    btn_text: str
    input_bg: str
    input_border: str
    bar_bg: str
    bar_border: str
    ok_bg: str
    ok_text: str
    warn_bg: str
    warn_text: str
    bad_bg: str
    bad_text: str
    info_bg: str
    info_text: str
    danger_bg: str
    danger_border: str
    danger_text: str
    link: str
    scroll_handle: str
    overlay: str         # translucent dim used over thumbnails
    scrim: str           # dim painted over the window behind the overlay sheet (v0.16.13)
    heart: str           # the Support button's lit heart and the Support window's big link (v0.27.0)


DARK = Palette(
    name="dark",
    base="#050a16", glow1="#0b4258", glow2="#1c2352",
    text="#eef3fb", muted="#8a96ad", faint="#5d6a82",
    accent="#2fc9e8", accent2="#7784ef", on_accent="#04202a",
    panel_bg="#1c2134", panel_border="#262d45",
    row_bg1="#12202e", row_bg2="#101b2a", row_border="#1b2d3d",
    row_exp1="#0e3646", row_exp2="#101d2c", row_exp_border="#1f5a6c",
    btn_bg="#252b3c", btn_border="#313b55", btn_hover="#2f3750", btn_text="#e6ecf7",
    input_bg="#151b31", input_border="#272f4a",
    bar_bg="#091924", bar_border="#133441",
    ok_bg="#0e4340", ok_text="#5de2b5",
    warn_bg="#443816", warn_text="#f0c24f",
    bad_bg="#4a1f2c", bad_text="#f4909f",
    info_bg="#12384a", info_text="#58cdea",
    danger_bg="#2b1822", danger_border="#5a2b3c", danger_text="#f08ea0",
    link="#4fc3f7", scroll_handle="#3a4768", overlay="#99050a16",
    scrim="#a8010409", heart="#ff5a7a",
)

LIGHT = Palette(
    name="light",
    base="#eef2f8", glow1="#bfe9f3", glow2="#d3d8fb",
    text="#0f1b2e", muted="#5b6a82", faint="#8a97ad",
    accent="#0fa6c9", accent2="#6575e6", on_accent="#ffffff",
    panel_bg="#ffffff", panel_border="#d7deea",
    row_bg1="#ffffff", row_bg2="#f6f8fc", row_border="#d9e1ee",
    row_exp1="#e3f6fb", row_exp2="#f3f8fd", row_exp_border="#9fd7e6",
    btn_bg="#e9eef6", btn_border="#cfd8e6", btn_hover="#dde5f1", btn_text="#13213a",
    input_bg="#f4f7fb", input_border="#d3dbe8",
    bar_bg="#ffffff", bar_border="#d3deea",
    ok_bg="#d6f5ea", ok_text="#0b7a58",
    warn_bg="#fdf0c9", warn_text="#8a6200",
    bad_bg="#fbdde3", bad_text="#a8203a",
    info_bg="#d9f1f8", info_text="#0a6f8a",
    danger_bg="#fdecef", danger_border="#efb3be", danger_text="#b3203d",
    link="#0a7fa8", scroll_handle="#b9c4d8", overlay="#99ffffff",
    scrim="#700f1b2e", heart="#e0284f",
)

_PALETTES = {"dark": DARK, "light": LIGHT}


class ThemeBus(QObject):
    changed = Signal(str)


bus = ThemeBus()
_current = DARK


def palette() -> Palette:
    return _current


def qcolor(hex_or_argb: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_or_argb)
    if alpha != 1.0:
        c.setAlphaF(alpha)
    return c


def init_from_settings() -> None:
    global _current
    _current = _PALETTES.get(ui_settings.get("theme"), DARK)


def set_theme(name: str, app=None) -> None:
    """Switch theme, persist it, restyle the app and tell custom-painted
    widgets to repaint."""
    global _current
    _current = _PALETTES.get(name, DARK)
    ui_settings.set_value("theme", _current.name)
    if app is not None:
        app.setStyleSheet(build_stylesheet())
    bus.changed.emit(_current.name)


def apply_app_font(app) -> None:
    """Application-wide default font (set in code, not QSS, so widgets
    that call setFont() / use base_font() are not overridden)."""
    f = base_font()
    f.setPixelSize(13)
    app.setFont(f)


def base_font(point_size: float = 10.0) -> QFont:
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSizeF(point_size)
    return f


def mono_font(point_size: float = 9.5) -> QFont:
    f = QFont()
    f.setFamilies(MONO_FAMILIES)
    f.setStyleHint(QFont.Monospace)
    f.setPointSizeF(point_size)
    return f


_QSS = Template(r"""
QWidget { color: $text; }
QMainWindow, QDialog { background: $base; }
QWidget#cmdwindow { background: $base; }
QLabel { background: transparent; }
QToolTip {
    background: $panel_bg; color: $text; border: 1px solid $panel_border;
    padding: 5px 8px; border-radius: 6px;
}

/* ---------- containers ---------- */
QFrame#panel {
    background: $panel_bg; border: 1px solid $panel_border; border-radius: 16px;
}
QFrame#row {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 $row_bg1, stop:1 $row_bg2);
    border: 1px solid $row_border; border-radius: 14px;
}
/* v0.17.0: an open bar's look (row_exp1 / row_exp2 / row_exp_border) is
   painted by the bar itself (ui_main_rows.paint_open_bar), so it can blend
   in as the bar opens. */
QFrame#row[pick="true"]:hover { border: 1px solid $danger_text; }
QFrame#bar {
    background: $bar_bg; border: 1px solid $bar_border; border-radius: 14px;
}
QFrame#trow { background: transparent; border: 1px solid transparent; border-radius: 12px; }
QFrame#trow:hover { background: $btn_bg; }
QFrame#trow[spacer="true"] { border: 1px dashed $row_border; }
QFrame#trow[selected="true"] { background: $btn_hover; border: 1px solid $accent; }
QFrame#trow[playing="true"] { border: 1px solid $accent2; }
QFrame#sep { background: $row_border; border: none; max-height: 1px; min-height: 1px; }
QFrame#inputbox {
    background: $input_bg; border: 1px solid $input_border; border-radius: 9px;
}
QFrame#overlaysheet {
    background: $base; border: 1px solid $panel_border; border-radius: 18px;
}

/* ---------- buttons ---------- */
QPushButton {
    background: $btn_bg; color: $btn_text; border: 1px solid $btn_border;
    border-radius: 9px; padding: 7px 16px; font-weight: 500;
}
QPushButton:hover { background: $btn_hover; }
QPushButton:pressed { background: $input_bg; }
QPushButton:disabled { color: $faint; background: $input_bg; border-color: $input_border; }
QPushButton[variant="primary"] {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 $accent, stop:1 $accent2);
    color: $on_accent; border: 1px solid transparent; font-weight: 600; padding: 8px 15px;
}
QPushButton[variant="primary"]:hover {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 $accent, stop:1 $accent2);
    border: 1px solid $text;
}
QPushButton[variant="primary"]:disabled { background: $input_bg; color: $faint; border: 1px solid $input_border; }
QPushButton[variant="danger"] {
    background: $danger_bg; color: $danger_text; border: 1px solid $danger_border;
}
QPushButton[variant="danger"]:hover { background: $bad_bg; }
QPushButton[variant="ghost"] { background: transparent; border: 1px solid transparent; color: $muted; }
QPushButton[variant="ghost"]:hover { background: $btn_bg; color: $text; }
QPushButton[variant="accent"] {
    background: $info_bg; color: $info_text; border: 1px solid $accent;
}
QPushButton[variant="accent"]:hover { background: $btn_hover; }
/* v0.27.0: the Support window's Ko-fi link (a pill) and its big "Support MMM" button */
QPushButton[variant="link"] {
    background: $panel_bg; border: 1px solid $panel_border; color: $link;
    padding: 7px 22px 7px 14px; font-size: 15px; font-weight: 600; border-radius: 22px;
}
QPushButton[variant="link"]:hover { border: 1px solid $link; text-decoration: underline; }
QPushButton[variant="support"] {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 $heart, stop:1 $accent2);
    border: 1px solid transparent; color: #ffffff;
    padding: 14px 40px; font-size: 26px; font-weight: 800; border-radius: 30px;
}
QPushButton[variant="support"]:hover { border: 2px solid #ffffff; }
QPushButton[variant="support"]:pressed { background: $heart; }
QPushButton[compact="true"] { padding: 4px 12px; font-size: 12px; border-radius: 8px; }
QPushButton[large="true"] { padding: 10px 22px; font-size: 15px; font-weight: 600; border-radius: 10px; }
QPushButton[segment="true"] {
    background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 6px 16px; color: $muted;
}
/* the selected pill is painted (and slid) by ui_widgets.SegmentedControl */
QPushButton[segment="true"]:checked { background: transparent; color: $text; }
QPushButton[segment="true"]:hover { color: $text; }

/* ---------- inputs ---------- */
QLineEdit {
    background: $input_bg; border: 1px solid $input_border; border-radius: 9px;
    padding: 7px 10px; selection-background-color: $accent; selection-color: $on_accent;
}
QLineEdit:focus { border: 1px solid $accent; }
/* v0.25.0: the Scheduled sync time (QTimeEdit) looks like the text fields */
QTimeEdit {
    background: $input_bg; border: 1px solid $input_border; border-radius: 9px;
    padding: 6px 10px; min-width: 64px; selection-background-color: $accent; selection-color: $on_accent;
}
QTimeEdit:focus { border: 1px solid $accent; }
QTimeEdit::up-button, QTimeEdit::down-button { width: 0px; border: none; }
QLineEdit[flat="true"] { background: transparent; border: 1px solid transparent; padding: 2px 4px; }
QLineEdit[flat="true"]:hover { border: 1px solid $input_border; }
QLineEdit[flat="true"]:focus { background: $input_bg; border: 1px solid $accent; }
QLineEdit[flat="true"][compact="true"] { padding: 0px 4px; }
QLineEdit[invalid="true"] { border: 1px solid $bad_text; }
QPlainTextEdit, QTextEdit {
    background: $input_bg; border: 1px solid $input_border; border-radius: 10px; padding: 6px;
    selection-background-color: $accent; selection-color: $on_accent;
}

/* ---------- text roles ---------- */
QLabel[role="muted"] { color: $muted; }
QLabel[role="faint"] { color: $faint; }
QLabel[role="caps"] { color: $accent; font-size: 11px; font-weight: 600; }
QLabel[role="capsmuted"] { color: $muted; font-size: 10.5px; font-weight: 600; }
QLabel[role="title"] { font-size: 28px; font-weight: 700; }
QLabel[role="h2"] { font-size: 15px; font-weight: 600; }
QLabel[role="link"] { color: $link; }
QLabel[role="keycap"] {
    background: $btn_bg; color: $text; border: 1px solid $btn_border; border-bottom-width: 2px;
    border-radius: 6px; padding: 1px 7px; font-size: 12px; font-weight: 600;
}
QLabel[role="ok"] { color: $ok_text; }
QLabel[role="warn"] { color: $warn_text; }
QLabel[role="bad"] { color: $bad_text; }

/* ---------- scrollbars ---------- */
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scroll_handle; border-radius: 3px; min-height: 36px; margin: 0 2px; }
QScrollBar::handle:vertical:hover { background: $accent; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: $scroll_handle; border-radius: 3px; min-width: 36px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ---------- slider (player scrub bar) ---------- */
QSlider::groove:horizontal { height: 6px; background: $input_border; border-radius: 3px; }
QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 $accent, stop:1 $accent2); border-radius: 3px;
}
QSlider::handle:horizontal {
    background: $text; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
    border: 2px solid $accent;
}
QSlider::handle:horizontal:disabled { background: $faint; border-color: $input_border; }
QSlider::sub-page:horizontal:disabled { background: $input_border; }

QMessageBox { background: $base; }
QMenu { background: $panel_bg; border: 1px solid $panel_border; padding: 4px; border-radius: 8px; }
QMenu::item { padding: 6px 18px; border-radius: 6px; }
QMenu::item:selected { background: $btn_hover; }
""")


def build_stylesheet() -> str:
    p = _current
    return _QSS.substitute({k: getattr(p, k) for k in p.__dataclass_fields__})
