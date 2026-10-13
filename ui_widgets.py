"""
Mud Music Manager - Reusable UI Widgets (v0.16.0)

Small custom-painted building blocks shared by every screen: the glowing
window background, thumbnails with a generated placeholder, platform
badges, glyph buttons (arrows, chevrons, transport controls), the
segmented sync progress bar, a circular spinner, a round checkbox, a
segmented option control, the player's speaker icon and the dimmed
in-app overlay that hosts Settings and the results pages.

Everything that paints itself reads colors from ui_theme.palette() and
repaints when the theme changes.

v0.17.0: short motion animations (see "Animation" below): drawers that
slide open without squashing what's inside (Drawer / DrawerSlide), a
glyph that can turn (the expand arrow), hover colours that blend and a
progress bar that fills smoothly. All are 120-260 ms and never delay or
block a click, key or task.

v0.17.1 (Checklist item 52): AppLogo shows Michael's logo (assets/,
dark / light by theme; see app_info.py).

v0.17.4 (Checklist item 55): smooth scrolling can be turned off in
Settings (smooth_scrolling_on(), ui_settings "smooth_scroll"); the other
animations are not affected.

v0.23.0: GroupIcon (the painted badge beside a Settings group's title:
visuals, user data, installation); SegmentedControl.clear_selection()
(Settings' page tabs are two controls, the pages and About, of which
only one shows a choice); OverlayHost sizes its sheet by the page's
OVERLAY_SIZE_FRACTION (Settings is larger than the results pages).
"""

import hashlib
import math
import os
from typing import List, Optional

from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QEvent, QObject, QPoint, QPointF, QPropertyAnimation, QRect, QRectF,
    QSize, Qt, QTimer, QVariantAnimation, Signal, Property,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

import app_info
import ui_settings
import ui_theme
from ui_theme import palette, qcolor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ThemeWatcher(QObject):
    """Child object that repaints its parent widget when the theme
    changes. Being a child, it (and the connection) disappears with the
    widget -- no calls into deleted widgets."""

    def __init__(self, widget):
        super().__init__(widget)
        self._widget = widget
        ui_theme.bus.changed.connect(self.on_changed)

    def on_changed(self, _name):
        self._widget.update()


def _watch_theme(widget) -> None:
    _ThemeWatcher(widget)


# ---------------------------------------------------------------------------
# Animation (v0.17.0)
# ---------------------------------------------------------------------------
# Motion, not fades: things that open slide open, arrows turn, colours
# blend while they move. Every animation is short and purely visual -- the
# change it shows has already happened (a bar is already "expanded", a
# panel already shown), so nothing waits for one to finish and clicks /
# keys are never held up. Setting the environment variable
# MMM_NO_ANIMATIONS=1 turns them all off (e.g. for headless checks).

ANIM_FAST_MS = 120   # hover colours
ANIM_OPEN_MS = 240   # a drawer opening (a playlist's details, the progress panel)
ANIM_CLOSE_MS = 200  # ...and closing
ANIM_BAR_MS = 260    # the progress bar filling up
MAX_HEIGHT = 16777215  # QWIDGETSIZE_MAX: no height limit

ANIMATIONS = os.environ.get("MMM_NO_ANIMATIONS", "").strip() != "1"


def animations_on() -> bool:
    return ANIMATIONS


def motion_curve() -> QEasingCurve:
    """Starts gently, travels, and settles softly (a cubic-bezier(0.3, 0,
    0.2, 1)). Unlike OutCubic it doesn't lurch most of the way in the first
    frames and then crawl."""
    curve = QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.3, 0.0), QPointF(0.2, 1.0), QPointF(1.0, 1.0))
    return curve


def mix_color(a: QColor, b: QColor, t: float) -> QColor:
    """a at t=0, b at t=1 (alpha included)."""
    t = max(0.0, min(1.0, t))
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


def make_value_anim(parent: QObject, on_value, duration: int = ANIM_FAST_MS,
                    curve=QEasingCurve.OutCubic) -> QVariantAnimation:
    """A reusable float animation that calls on_value(v) every frame."""
    anim = QVariantAnimation(parent)
    anim.setDuration(duration)
    anim.setEasingCurve(curve)
    anim.valueChanged.connect(lambda v: on_value(float(v)))
    return anim


def run_value_anim(anim: QVariantAnimation, start: float, end: float) -> None:
    anim.stop()
    anim.setStartValue(float(start))
    anim.setEndValue(float(end))
    anim.start()


class Drawer(QWidget):
    """Holds one content widget and shows `reveal` (0..1) of its height.

    The content always keeps its full natural size and is only clipped, so
    nothing inside it is squashed or shuffled while the drawer moves (the
    v0.16 slide shrank the widget itself, and its labels and buttons were
    pressed together as it opened). anchor:
      "bottom": the content's bottom edge follows the drawer's bottom, so
                it slides down from under the line above (a playlist's
                details);
      "top":    the content's top edge follows the drawer's top, so in a
                layout that grows upward it rises from below (the progress
                panel, its Queue list)."""

    def __init__(self, content: QWidget, anchor: str = "bottom", parent=None):
        super().__init__(parent)
        self._content = content
        self._anchor = anchor
        self._reveal = 1.0
        content.setParent(self)
        content.installEventFilter(self)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    @property
    def content(self) -> QWidget:
        return self._content

    def reveal(self) -> float:
        return self._reveal

    def full_height(self) -> int:
        """The content's height: the drawer's height once fully open."""
        return self._content_height()

    def set_reveal(self, t: float) -> None:
        self._reveal = max(0.0, min(1.0, t))
        self.updateGeometry()
        self._place()

    def _content_height(self) -> int:
        c = self._content
        if c.isHidden():
            return 0
        if c.hasHeightForWidth() and self.width() > 0:
            return c.heightForWidth(self.width())
        return c.sizeHint().height()

    def sizeHint(self) -> QSize:
        return QSize(self._content.sizeHint().width(), round(self._content_height() * self._reveal))

    def minimumSizeHint(self) -> QSize:
        # as narrow as the content allows (v0.16.8: rows must never force
        # the list wider), as tall as the part showing
        return QSize(self._content.minimumSizeHint().width(), self.sizeHint().height())

    def _place(self) -> None:
        h = self._content_height()
        y = self.height() - h if self._anchor == "bottom" else 0
        self._content.setGeometry(0, y, self.width(), h)

    def resizeEvent(self, e):
        self._place()
        super().resizeEvent(e)

    def eventFilter(self, obj, e):
        # the content's own layout changed (text, a row added): follow it
        if obj is self._content and e.type() == QEvent.LayoutRequest:
            self.updateGeometry()
            self._place()
        return False


class DrawerSlide(QObject):
    """Opens / closes a Drawer with the motion curve: shown at the start of
    opening, hidden at the end of closing. Called again mid-slide it turns
    around from where it is (taking only the time the rest of the way
    needs). on_value(t) is called on every frame with the open fraction,
    so other things can move with it (an arrow turning, a colour)."""

    def __init__(self, drawer: Drawer, open_ms: int = ANIM_OPEN_MS, close_ms: int = ANIM_CLOSE_MS,
                 on_value=None):
        super().__init__(drawer)
        self._drawer = drawer
        self._open_ms, self._close_ms = open_ms, close_ms
        self._on_value = on_value
        self._t = 0.0
        self._target = 0.0
        self._anim = make_value_anim(self, self._apply, open_ms, motion_curve())
        self._anim.finished.connect(self._finished)
        drawer.set_reveal(0.0)
        drawer.hide()

    @property
    def t(self) -> float:
        return self._t

    def is_open(self) -> bool:
        return self._target == 1.0

    def is_closing(self) -> bool:
        return self._target == 0.0 and self._anim.state() == QAbstractAnimation.Running

    def open(self, animate: bool = True) -> None:
        self._go(1.0, animate)

    def close(self, animate: bool = True) -> None:
        self._go(0.0, animate)

    def _go(self, target: float, animate: bool) -> None:
        running = self._anim.state() == QAbstractAnimation.Running
        if target == self._target and (running or self._t == target):
            return
        self._target = target
        self._anim.stop()
        d = self._drawer
        if target == 1.0:
            d.show()
        if not (animate and ANIMATIONS and d.isVisible()):
            self._apply(target)
            self._finished()
            return
        full = self._open_ms if target == 1.0 else self._close_ms
        self._anim.setDuration(max(60, round(full * abs(target - self._t))))
        run_value_anim(self._anim, self._t, target)

    def _apply(self, t: float) -> None:
        self._t = t
        self._drawer.set_reveal(t)
        if self._on_value is not None:
            self._on_value(t)

    def _finished(self) -> None:
        if self._target == 0.0 and self._t <= 0.0:
            self._drawer.hide()


ANIM_SCROLL_MS = 220  # one wheel notch / a keyboard scroll gliding into place


def smooth_scrolling_on() -> bool:
    """Settings -> Smooth scrolling (v0.17.4, ui_settings "smooth_scroll",
    on by default). Read on every use, so a change applies at once."""
    return ANIMATIONS and ui_settings.get("smooth_scroll") is not False


class SmoothScroll(QObject):
    """Smooth vertical scrolling for a scroll area (v0.17.0).

    A mouse-wheel notch scrolls the same distance Qt always did (wheel
    lines x the scroll bar's single step), but glides there; notches in
    quick succession add up and keep gliding. A touchpad's pixel scrolling
    is already smooth and is left to Qt. Code can glide too, with
    scroll_to() or the module's ensure_visible() / scroll_area_to(). Dragging
    the scroll bar, or anything else moving it, stops a glide at once.
    v0.17.4: with Smooth scrolling off (smooth_scrolling_on()), the wheel
    is left to Qt and scroll_to() jumps, exactly as without this object."""

    def __init__(self, area: QWidget, duration: int = ANIM_SCROLL_MS):
        super().__init__(area)
        self._area = area
        self._sb = area.verticalScrollBar()
        self._own = False      # this object is moving the bar
        self._target: Optional[int] = None
        self._anim = make_value_anim(self, self._step, duration, QEasingCurve.OutCubic)
        self._anim.finished.connect(self._done)
        self._duration = duration
        area.viewport().installEventFilter(self)
        self._sb.valueChanged.connect(self._value_changed)
        self._sb.sliderPressed.connect(self.stop)
        area._mmm_smooth = self

    def gliding(self) -> bool:
        return self._anim.state() == QAbstractAnimation.Running

    def heading(self) -> int:
        """Where the bar is going (where it is, when not gliding)."""
        return self._target if (self._target is not None and self.gliding()) else self._sb.value()

    def stop(self) -> None:
        self._anim.stop()
        self._target = None

    def scroll_to(self, value: float, clamp: bool = True) -> None:
        """Glide to `value`. clamp=False lets the target lie past the
        current end, for a list that is about to grow (a bar opening)."""
        sb = self._sb
        value = int(round(value))
        if clamp:
            value = max(sb.minimum(), min(value, sb.maximum()))
        if not smooth_scrolling_on() or not self._area.isVisible():
            self.stop()
            sb.setValue(value)
            return
        if self.gliding() and self._target == value:
            return
        if value == sb.value():
            self.stop()
            return
        self._target = value
        run_value_anim(self._anim, sb.value(), value)

    def _step(self, v: float) -> None:
        self._own = True
        try:
            self._sb.setValue(int(round(v)))
        finally:
            self._own = False

    def _done(self) -> None:
        self._target = None

    def _value_changed(self, _v) -> None:
        if not self._own and self.gliding():
            self.stop()  # the user dragged it, or the list changed under it

    def eventFilter(self, obj, e):
        if e.type() != QEvent.Wheel or obj is not self._area.viewport():
            return False
        if not smooth_scrolling_on():
            self.stop()
            return False
        if e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier):
            return False
        if not e.pixelDelta().isNull():  # touchpad: already smooth
            self.stop()
            return False
        dy = e.angleDelta().y()
        sb = self._sb
        if dy == 0 or sb.maximum() <= sb.minimum():
            return False
        step = QApplication.wheelScrollLines() * sb.singleStep()
        base = self.heading()
        new = max(sb.minimum(), min(int(round(base - dy / 120 * step)), sb.maximum()))
        if new == base and not self.gliding():
            return False  # already at the end: let it go on as Qt would
        self.scroll_to(new)
        return True


def smooth_scroll(area: QWidget) -> SmoothScroll:
    """Give a scroll area smooth scrolling (once)."""
    existing = getattr(area, "_mmm_smooth", None)
    return existing if existing is not None else SmoothScroll(area)


def scroll_area_to(area: QWidget, value: float) -> None:
    """Move a scroll area's vertical bar to `value`, gliding if it has
    smooth scrolling."""
    smooth = getattr(area, "_mmm_smooth", None)
    if smooth is not None:
        smooth.scroll_to(value)
    else:
        area.verticalScrollBar().setValue(int(round(value)))


def scroll_area_by(area: QWidget, dy: int) -> bool:
    """Scroll by dy from where the bar is heading; False if it can't move."""
    smooth = getattr(area, "_mmm_smooth", None)
    sb = area.verticalScrollBar()
    base = smooth.heading() if smooth is not None else sb.value()
    new = max(sb.minimum(), min(base + dy, sb.maximum()))
    if new == base:
        return False
    scroll_area_to(area, new)
    return True


def ensure_visible(area: QWidget, w: QWidget, xmargin: int = 50, ymargin: int = 50) -> None:
    """QScrollArea.ensureWidgetVisible, but the vertical part glides when
    the area has smooth scrolling. The content is laid out first, so a
    widget just added or moved is measured where it will be."""
    smooth = getattr(area, "_mmm_smooth", None)
    content = area.widget() if hasattr(area, "widget") else None
    if smooth is None or content is None or not smooth_scrolling_on():
        if smooth is not None:
            smooth.stop()
        area.ensureWidgetVisible(w, xmargin, ymargin)
        return
    if content.layout() is not None:
        content.layout().activate()
    pos = w.mapTo(content, QPoint(0, 0))
    top, h = pos.y(), w.height()
    vh = area.viewport().height()
    cur = smooth.heading()
    if top - ymargin < cur:
        target = top - ymargin
    elif top + h + ymargin > cur + vh:
        target = min(top - ymargin, top + h + ymargin - vh)  # a tall widget keeps its top in view
    else:
        target = None
    if target is not None:
        smooth.scroll_to(target)
    hsb = area.horizontalScrollBar()
    if hsb.maximum() > 0:
        left, cw, vw = pos.x(), w.width(), area.viewport().width()
        if left - xmargin < hsb.value():
            hsb.setValue(left - xmargin)
        elif left + cw + xmargin > hsb.value() + vw:
            hsb.setValue(min(left - xmargin, left + cw + xmargin - vw))


def format_duration(seconds: float, long_form: bool = False) -> str:
    """3:41 for a track; '4h 12m' for a playlist total (long_form)."""
    seconds = max(0, int(round(seconds or 0)))
    if long_form:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        if h:
            return f"{h}h {m:02d}m" if m else f"{h}h 00m"
        return f"{m}m"
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def placeholder_pixmap(name: str, size: int = 96) -> QPixmap:
    """Generated placeholder thumbnail for a playlist (or track): a
    rounded gradient tile in a color derived from the name, with its
    initials. Same name -> same look, so it is stable between runs."""
    digest = hashlib.md5((name or "?").encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    c1 = QColor.fromHsl(hue, 150, 105)
    c2 = QColor.fromHsl((hue + 38) % 360, 165, 70)

    words = [w for w in (name or "?").replace("-", " ").replace("_", " ").split() if w]
    initials = ("".join(w[0] for w in words[:2]) or "?").upper()

    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, c1)
    grad.setColorAt(1, c2)
    p.setBrush(QBrush(grad))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    p.setPen(QColor(255, 255, 255, 235))
    f = ui_theme.base_font(size * 0.30 * 0.75)
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, initials)
    p.end()
    return pm


# ---------------------------------------------------------------------------
# Window background
# ---------------------------------------------------------------------------

class GlowBackground(QWidget):
    """Window background: the base color with a teal glow in the top-left
    and an indigo glow in the bottom-right, like the appearance mock."""

    def __init__(self, parent=None):
        super().__init__(parent)
        _watch_theme(self)

    def paintEvent(self, _event):
        p = QPainter(self)
        pal = palette()
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(pal.base))

        r = max(w, h)
        g1 = QRadialGradient(QPointF(w * 0.10, -h * 0.08), r * 0.62)
        c = QColor(pal.glow1)
        c.setAlphaF(0.95 if pal.name == "dark" else 0.75)
        g1.setColorAt(0.0, c)
        c0 = QColor(pal.glow1)
        c0.setAlpha(0)
        g1.setColorAt(1.0, c0)
        p.fillRect(self.rect(), QBrush(g1))

        g2 = QRadialGradient(QPointF(w * 0.98, h * 1.05), r * 0.55)
        c = QColor(pal.glow2)
        c.setAlphaF(0.95 if pal.name == "dark" else 0.7)
        g2.setColorAt(0.0, c)
        c0 = QColor(pal.glow2)
        c0.setAlpha(0)
        g2.setColorAt(1.0, c0)
        p.fillRect(self.rect(), QBrush(g2))

        g3 = QRadialGradient(QPointF(w * 0.42, h * 1.12), r * 0.32)
        c = QColor(pal.glow1)
        c.setAlphaF(0.30)
        g3.setColorAt(0.0, c)
        c0 = QColor(pal.glow1)
        c0.setAlpha(0)
        g3.setColorAt(1.0, c0)
        p.fillRect(self.rect(), QBrush(g3))


# ---------------------------------------------------------------------------
# Text widgets
# ---------------------------------------------------------------------------

class CapsLabel(QLabel):
    """Small, letter-spaced uppercase section label ('ACTIONS')."""

    def __init__(self, text: str = "", muted: bool = False, parent=None):
        super().__init__(parent)
        self.setProperty("role", "capsmuted" if muted else "caps")
        f = self.font()
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.6)
        f.setBold(True)
        self.setFont(f)
        self.setText(text.upper())


class PillLabel(QLabel):
    """Rounded status pill, custom-painted (so the fully-rounded shape
    is identical on every platform/theme). tone: ok / warn / bad / info /
    neutral. dot=True adds a leading status dot. font_px sets the text size
    (side padding grows with it)."""

    def __init__(self, text: str = "", tone: str = "neutral", dot: bool = False, height: int = 22,
                 parent=None, font_px: int = 12):
        super().__init__(text, parent)
        self._tone = tone
        self._dot = dot
        self._h = height
        self._font_px = font_px
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        _watch_theme(self)

    def set_tone(self, tone: str) -> None:
        self._tone = tone
        self.update()

    def setText(self, text: str) -> None:
        super().setText(text)
        self.updateGeometry()

    def _font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(self._font_px)
        f.setBold(True)
        return f

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self._font())
        extra = 22 + 2 * (self._font_px - 12) + (14 if self._dot else 0)
        return QSize(fm.horizontalAdvance(self.text()) + extra, self._h)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _event):
        pal = palette()
        bg, fg = {
            "ok": (pal.ok_bg, pal.ok_text), "warn": (pal.warn_bg, pal.warn_text),
            "bad": (pal.bad_bg, pal.bad_text), "info": (pal.info_bg, pal.info_text),
        }.get(self._tone, (pal.btn_bg, pal.muted))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setFont(self._font())
        p.setPen(QColor(fg))
        text_rect = QRectF(r)
        if self._dot:
            p.setBrush(QColor(fg))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(r.left() + 13, r.center().y()), 3.2, 3.2)
            text_rect.setLeft(r.left() + 21)
            p.setPen(QColor(fg))
        p.drawText(text_rect, Qt.AlignCenter, self.text())


class ElidedLabel(QLabel):
    """Single-line label that elides with '...' instead of forcing the
    layout wider."""

    def __init__(self, text: str = "", parent=None, mode=Qt.ElideRight):
        super().__init__(parent)
        self._full = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        super().setText(text)

    def setText(self, text: str) -> None:
        self._full = text or ""
        self.update()

    def text(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(min(fm.horizontalAdvance(self._full) + 4, 600), fm.height() + 2)

    def minimumSizeHint(self) -> QSize:
        return QSize(10, QFontMetrics(self.font()).height() + 2)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setFont(self.font())
        p.setPen(self.palette().color(self.foregroundRole()))
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(self._full, self._mode, max(0, self.width()))
        p.drawText(self.rect(), int(self.alignment()) | Qt.AlignVCenter, elided)


class ClickableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class ClickableElidedLabel(ElidedLabel):
    """ElidedLabel that emits clicked() (e.g. a link that must shrink
    with the window instead of forcing it wider)."""

    clicked = Signal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


# ---------------------------------------------------------------------------
# Logo, platform badge, thumbnail
# ---------------------------------------------------------------------------

class AppLogo(QWidget):
    """The app logo. v0.17.1 (Checklist item 52): Michael's logo,
    assets/logo_dark.png on the dark theme and assets/logo_light.png on
    the light one (app_info.logo_path()), swapped when the theme changes.
    The 1024 px image is scaled down once per theme / size / screen scale
    with smooth (area-averaged) scaling, which stays crisp at header size.
    If the file is missing, the old painted vinyl-style 'M' is drawn."""

    _sources = {}  # theme -> full-size QPixmap (None if missing), shared by every logo

    def __init__(self, size: int = 46, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._scaled = None
        self._scaled_key = None
        self.setToolTip(f"{app_info.APP_NAME}\n{app_info.COPYRIGHT}")
        _watch_theme(self)

    @classmethod
    def _source(cls, theme: str):
        if theme not in cls._sources:
            path = app_info.logo_path(theme)
            pm = QPixmap(path) if os.path.exists(path) else None
            cls._sources[theme] = pm if pm is not None and not pm.isNull() else None
        return cls._sources[theme]

    def _pixmap(self):
        theme = palette().name
        dpr = self.devicePixelRatioF()
        key = (theme, self.width(), dpr)
        if key != self._scaled_key:
            src = self._source(theme)
            self._scaled = None
            if src is not None:
                px = max(1, round(self.width() * dpr))
                self._scaled = src.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._scaled.setDevicePixelRatio(dpr)
            self._scaled_key = key
        return self._scaled

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        s = self.width()
        pm = self._pixmap()
        if pm is not None:
            p.drawPixmap(0, 0, pm)
            return
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)
        p.setClipPath(clip)
        pal = palette()
        p.fillRect(QRectF(0, 0, s, s), QColor("#0b1220"))
        p.setPen(QPen(QColor(pal.accent), s * 0.045))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(s * 0.12, s * 0.12, s * 0.76, s * 0.76))
        p.setPen(QPen(QColor(255, 255, 255, 40), s * 0.02))
        p.drawEllipse(QRectF(s * 0.22, s * 0.22, s * 0.56, s * 0.56))
        f = ui_theme.base_font(s * 0.36 * 0.75)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(QRectF(0, 0, s, s), Qt.AlignCenter, "M")


_PLATFORM_TIPS = {"spotify": "Spotify playlist", "youtube": "YouTube playlist",
                  "ytmusic": "YT Music playlist", "soundcloud": "SoundCloud playlist",
                  "applemusic": "Apple Music playlist", "amazonmusic": "Amazon Music playlist"}

# Letter badges of the platforms added with Checklist item 42: (fill, text).
_LETTER_BADGES = {"soundcloud": ("#ff5500", "SC"),    # v0.16.52
                  "applemusic": ("#fa2d48", "AM"),    # v0.16.53, Apple Music's red
                  "amazonmusic": ("#00a8e1", "AMZ")}  # v0.16.54, Amazon Music's blue


class PlatformBadge(QWidget):
    """Round source badge: green SPT (Spotify), red YT (YouTube) or
    (v0.16.51, Checklist item 42a) YTM (YT Music): red with a white ring,
    after YT Music's own logo, so it reads apart from YouTube's. v0.16.52
    (Checklist item 42b): SC (SoundCloud), white on SoundCloud's orange.
    v0.16.53 (42c): AM (Apple Music), white on Apple Music's red.
    v0.16.54 (42d): AMZ (Amazon Music), white on Amazon Music's blue."""

    def __init__(self, platform: str = "spotify", size: int = 28, parent=None):
        super().__init__(parent)
        self._platform = platform
        self.setFixedSize(size, size)
        self.setToolTip(_PLATFORM_TIPS.get(platform, "Spotify playlist"))

    def set_platform(self, platform: str) -> None:
        self._platform = platform
        self.setToolTip(_PLATFORM_TIPS.get(platform, "Spotify playlist"))
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self.width()
        if self._platform in _LETTER_BADGES:
            fill, text = _LETTER_BADGES[self._platform]
            p.setBrush(QColor(fill))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(0.5, 0.5, s - 1, s - 1))
            f = ui_theme.base_font(s * (0.27 if len(text) > 2 else 0.32) * 0.75)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(0, 0, s, s), Qt.AlignCenter, text)
            return
        ytm = self._platform == "ytmusic"
        yt = self._platform == "youtube" or ytm
        p.setBrush(QColor("#ff0000") if yt else QColor("#1ed760"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(0.5, 0.5, s - 1, s - 1))
        if ytm:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#ffffff"), max(1.0, s * 0.055)))
            p.drawEllipse(QRectF(s * 0.1, s * 0.1, s * 0.8, s * 0.8))
        f = ui_theme.base_font(s * (0.27 if ytm else 0.32) * 0.75)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#ffffff") if yt else QColor("#04210f"))
        p.drawText(QRectF(0, 0, s, s), Qt.AlignCenter, "YTM" if ytm else ("YT" if yt else "SPT"))


class AlbumBadge(QWidget):
    """Album icon (v0.16.48, Checklist item 41a): a small painted vinyl
    record with an accent-coloured label, shown beside the platform badge
    on an album's Main Menu bar."""

    def __init__(self, size: int = 26, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setToolTip("Album")
        _watch_theme(self)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self.width()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#15171c"))
        p.drawEllipse(QRectF(0.5, 0.5, s - 1, s - 1))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 38), max(1.0, s * 0.03)))
        for r in (0.40, 0.33):
            p.drawEllipse(QRectF(s * (0.5 - r), s * (0.5 - r), s * 2 * r, s * 2 * r))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(palette().accent))
        p.drawEllipse(QRectF(s * 0.31, s * 0.31, s * 0.38, s * 0.38))
        p.setBrush(QColor("#15171c"))
        p.drawEllipse(QRectF(s * 0.455, s * 0.455, s * 0.09, s * 0.09))


class ThumbLabel(QWidget):
    """Rounded thumbnail. Shows a pixmap if one is set, otherwise a
    generated placeholder made from `placeholder_name`. Optional hover
    overlay: 'edit' (pencil, playlist icon change) or 'play' (a play
    triangle, track rows)."""

    clicked = Signal()

    def __init__(self, size: int = 44, radius: float = 0.2, overlay: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._radius = radius
        self._overlay = overlay
        self._pm: Optional[QPixmap] = None
        self._placeholder_name = "?"
        self._placeholder_pm: Optional[QPixmap] = None
        self._hover = False
        if overlay:
            self.setCursor(Qt.PointingHandCursor)
            self.setMouseTracking(True)

    def set_placeholder_name(self, name: str) -> None:
        self._placeholder_name = name or "?"
        self._placeholder_pm = None
        self.update()

    def set_pixmap(self, pm: Optional[QPixmap]) -> None:
        self._pm = pm if pm and not pm.isNull() else None
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if self._overlay and e.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        s = self.width()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, s, s), s * self._radius, s * self._radius)
        p.setClipPath(clip)

        if self._pm:
            scaled = self._pm.scaled(QSize(s * 2, s * 2), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            sw, sh = scaled.width(), scaled.height()
            p.drawPixmap(QRectF(0, 0, s, s), scaled, QRectF((sw - s * 2) / 2, (sh - s * 2) / 2, s * 2, s * 2))
        else:
            if self._placeholder_pm is None:
                self._placeholder_pm = placeholder_pixmap(self._placeholder_name, max(s, 48))
            p.drawPixmap(QRectF(0, 0, s, s), self._placeholder_pm, QRectF(self._placeholder_pm.rect()))

        if self._overlay and self._hover:
            p.fillRect(QRectF(0, 0, s, s), QColor(0, 0, 0, 140))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 240))
            if self._overlay == "play":
                path = QPainterPath()
                path.moveTo(s * 0.38, s * 0.28)
                path.lineTo(s * 0.74, s * 0.5)
                path.lineTo(s * 0.38, s * 0.72)
                path.closeSubpath()
                p.drawPath(path)
            else:  # edit: a small pencil
                p.save()
                p.translate(s / 2, s / 2)
                p.rotate(-45)
                p.drawRoundedRect(QRectF(-s * 0.08, -s * 0.30, s * 0.16, s * 0.42), 2, 2)
                tip = QPainterPath()
                tip.moveTo(-s * 0.08, s * 0.14)
                tip.lineTo(s * 0.08, s * 0.14)
                tip.lineTo(0, s * 0.28)
                tip.closeSubpath()
                p.drawPath(tip)
                p.restore()


# ---------------------------------------------------------------------------
# Glyph buttons
# ---------------------------------------------------------------------------

class GlyphButton(QAbstractButton):
    """Paint-it-yourself icon button. kind: up, down, chev_up, chev_down,
    prev, next, play, pause, cmd, gear, close. ring=True draws a circular
    outline (the big player buttons).

    height (v0.16.34, Checklist item 35): a button shorter than it is wide
    (unboxed, no ring). The glyph keeps the size `size` gives it and is
    centred in the shorter button; only the empty space around it goes."""

    def __init__(self, kind: str, size: int = 34, ring: bool = False, boxed: bool = False, parent=None,
                 height: Optional[int] = None):
        super().__init__(parent)
        self.kind = kind
        self._ring = ring
        self._boxed = boxed
        self.setFixedSize(size, height or size)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._opacity = 1.0
        # v0.17.0: hover colours fade in / out (0 = not hovered, 1 = hovered),
        # and fade_to() fades the whole glyph (a playlist bar's chevron).
        self._hover_t = 0.0
        self._hover_anim: Optional[QVariantAnimation] = None
        self._fade_anim: Optional[QVariantAnimation] = None
        self._rotation = 0.0  # degrees, clockwise (set_rotation)

    def set_rotation(self, degrees: float) -> None:
        """Turn the glyph about its centre (v0.17.0: a playlist bar's arrow
        turns from down to up as the bar opens)."""
        self._rotation = degrees
        self.update()

    def _set_hover_t(self, v: float) -> None:
        self._hover_t = v
        self.update()

    def _set_opacity(self, v: float) -> None:
        self._opacity = v
        self.update()

    def _animate_hover(self, on: bool) -> None:
        target = 1.0 if on else 0.0
        if not ANIMATIONS or not self.isVisible():
            if self._hover_anim is not None:
                self._hover_anim.stop()
            self._set_hover_t(target)
            return
        if self._hover_anim is None:
            self._hover_anim = make_value_anim(self, self._set_hover_t)
        run_value_anim(self._hover_anim, self._hover_t, target)

    def fade_to(self, opacity: float) -> None:
        """Fade the glyph to this opacity (v0.17.0)."""
        if self._fade_anim is not None and self._fade_anim.state() == QAbstractAnimation.Running \
                and self._fade_anim.endValue() == opacity:
            return
        if not ANIMATIONS or not self.isVisible():
            if self._fade_anim is not None:
                self._fade_anim.stop()
            self._set_opacity(opacity)
            return
        if self._fade_anim is None:
            self._fade_anim = make_value_anim(self, self._set_opacity)
        run_value_anim(self._fade_anim, self._opacity, opacity)

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.update()

    def sizeHint(self):
        return self.size()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = palette()
        s = self.width()
        if self.height() != s:
            p.translate(0, (self.height() - s) / 2)  # keep the glyph centred
        enabled = self.isEnabled()
        hover = self._hover_t if enabled else 0.0  # v0.17.0: fades between 0 and 1
        col = mix_color(QColor(pal.muted), QColor(pal.text), hover)
        if self.kind in ("play", "pause", "prev", "next") and enabled:
            col = QColor(pal.text)
        if not enabled:
            col = QColor(pal.faint)
        col.setAlphaF(col.alphaF() * self._opacity)

        if self._boxed:
            p.setPen(QPen(QColor(pal.btn_border), 1))
            p.setBrush(mix_color(QColor(pal.btn_bg), QColor(pal.btn_hover), hover))
            p.drawRoundedRect(QRectF(0.5, 0.5, s - 1, s - 1), 10, 10)
        if self._ring:
            ring_col = mix_color(QColor(pal.muted), QColor(pal.accent), hover)
            if not enabled:
                ring_col = QColor(pal.faint)
            p.setPen(QPen(ring_col, 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QRectF(1.5, 1.5, s - 3, s - 3))

        p.setPen(Qt.NoPen)
        p.setBrush(col)
        k = self.kind
        if self._rotation:
            p.translate(s / 2, s / 2)
            p.rotate(self._rotation)
            p.translate(-s / 2, -s / 2)

        def tri(points):
            path = QPainterPath()
            path.moveTo(*points[0])
            for pt in points[1:]:
                path.lineTo(*pt)
            path.closeSubpath()
            p.drawPath(path)

        if k == "up":
            tri([(s * .28, s * .62), (s * .72, s * .62), (s * .5, s * .30)])
        elif k == "down":
            tri([(s * .28, s * .38), (s * .72, s * .38), (s * .5, s * .70)])
        elif k in ("chev_up", "chev_down"):
            pen = QPen(col, max(1.6, s * 0.075))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            path = QPainterPath()
            if k == "chev_up":
                path.moveTo(s * .30, s * .60)
                path.lineTo(s * .5, s * .40)
                path.lineTo(s * .70, s * .60)
            else:
                path.moveTo(s * .30, s * .40)
                path.lineTo(s * .5, s * .60)
                path.lineTo(s * .70, s * .40)
            p.drawPath(path)
        elif k == "play":
            tri([(s * .40, s * .30), (s * .72, s * .5), (s * .40, s * .70)])
        elif k == "pause":
            p.drawRoundedRect(QRectF(s * .34, s * .30, s * .11, s * .40), 2, 2)
            p.drawRoundedRect(QRectF(s * .55, s * .30, s * .11, s * .40), 2, 2)
        elif k == "prev":
            p.drawRoundedRect(QRectF(s * .30, s * .30, s * .07, s * .40), 1.5, 1.5)
            tri([(s * .70, s * .30), (s * .70, s * .70), (s * .40, s * .5)])
        elif k == "next":
            p.drawRoundedRect(QRectF(s * .63, s * .30, s * .07, s * .40), 1.5, 1.5)
            tri([(s * .30, s * .30), (s * .30, s * .70), (s * .60, s * .5)])
        elif k == "close":
            pen = QPen(col, max(1.6, s * 0.075))
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(s * .32, s * .32), QPointF(s * .68, s * .68))
            p.drawLine(QPointF(s * .68, s * .32), QPointF(s * .32, s * .68))
        elif k == "cmd":
            pen = QPen(col, max(1.6, s * 0.06))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            path = QPainterPath()
            path.moveTo(s * .27, s * .33)
            path.lineTo(s * .43, s * .48)
            path.lineTo(s * .27, s * .63)
            p.drawPath(path)
            p.drawLine(QPointF(s * .50, s * .66), QPointF(s * .72, s * .66))
        elif k == "gear":
            pen = QPen(col, max(1.6, s * 0.06))
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            c = QPointF(s / 2, s / 2)
            r_in, r_out = s * 0.16, s * 0.26
            teeth = 8
            path = QPainterPath()
            for i in range(teeth * 2):
                ang = math.pi * i / teeth
                rad = r_out + s * 0.06 if i % 2 == 0 else r_out
                a0 = ang - math.pi / (teeth * 2) * 0.55
                a1 = ang + math.pi / (teeth * 2) * 0.55
                for a in (a0, a1):
                    pt = QPointF(c.x() + math.cos(a) * rad, c.y() + math.sin(a) * rad)
                    if path.elementCount() == 0:
                        path.moveTo(pt)
                    else:
                        path.lineTo(pt)
            path.closeSubpath()
            p.drawPath(path)
            p.drawEllipse(c, r_in, r_in)

    def enterEvent(self, e):
        self._animate_hover(True)

    def leaveEvent(self, e):
        self._animate_hover(False)

    def hideEvent(self, e):
        # a button hidden under the mouse gets no leave event; reset it
        if self._hover_anim is not None:
            self._hover_anim.stop()
        self._hover_t = 0.0
        super().hideEvent(e)


class SpeakerIcon(QWidget):
    """Painted speaker shown beside the player's volume slider (v0.16.9).
    Not a button: it only reflects the level -- a cross at 0, one wave
    below half, two waves from half up."""

    def __init__(self, size: int = 22, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._level = 1.0
        _watch_theme(self)

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, level))
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = palette()
        s = self.width()
        col = QColor(pal.muted if self.isEnabled() else pal.faint)

        # speaker body: a small box plus the flared cone
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        path = QPainterPath()
        path.moveTo(s * .12, s * .38)
        path.lineTo(s * .28, s * .38)
        path.lineTo(s * .48, s * .20)
        path.lineTo(s * .48, s * .80)
        path.lineTo(s * .28, s * .62)
        path.lineTo(s * .12, s * .62)
        path.closeSubpath()
        p.drawPath(path)

        pen = QPen(col, max(1.4, s * 0.075))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if self._level <= 0.0:
            p.drawLine(QPointF(s * .62, s * .38), QPointF(s * .86, s * .62))
            p.drawLine(QPointF(s * .86, s * .38), QPointF(s * .62, s * .62))
            return
        # arcs centred on the cone's mouth; angles in 1/16 degree
        p.drawArc(QRectF(s * .38, s * .34, s * .26, s * .32), -50 * 16, 100 * 16)
        if self._level >= 0.5:
            p.drawArc(QRectF(s * .34, s * .18, s * .48, s * .64), -50 * 16, 100 * 16)


class GroupIcon(QWidget):
    """Painted badge beside a Settings group's title (v0.23.0): a rounded
    square in the accent gradient with a glyph in the on-accent colour.
    kind: 'visuals' (an eye), 'user_data' (a database cylinder),
    'install' (an arrow into a tray); v0.23.2: 'other' (three sliders),
    'download' (an arrow down out of a cloud). Not a button."""

    def __init__(self, kind: str, size: int = 36, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(size, size)
        _watch_theme(self)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = palette()
        s = self.width()
        r = QRectF(0.5, 0.5, s - 1, s - 1)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, QColor(pal.accent))
        g.setColorAt(1, QColor(pal.accent2))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(r, s * 0.28, s * 0.28)

        col = QColor(pal.on_accent)
        pen = QPen(col, max(1.6, s * 0.075))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        k = self.kind
        if k == "visuals":
            path = QPainterPath()
            path.moveTo(s * .18, s * .5)
            path.quadTo(s * .5, s * .16, s * .82, s * .5)
            path.quadTo(s * .5, s * .84, s * .18, s * .5)
            p.drawPath(path)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(s * .5, s * .5), s * .11, s * .11)
        elif k == "user_data":
            left, right, w = s * .27, s * .73, s * .46
            p.drawEllipse(QRectF(left, s * .20, w, s * .15))
            p.drawLine(QPointF(left, s * .275), QPointF(left, s * .72))
            p.drawLine(QPointF(right, s * .275), QPointF(right, s * .72))
            # lower halves of the middle and bottom rims (angles in 1/16 degree)
            p.drawArc(QRectF(left, s * .42, w, s * .15), 180 * 16, 180 * 16)
            p.drawArc(QRectF(left, s * .645, w, s * .15), 180 * 16, 180 * 16)
        elif k == "install":
            p.drawLine(QPointF(s * .5, s * .20), QPointF(s * .5, s * .58))
            head = QPainterPath()
            head.moveTo(s * .35, s * .44)
            head.lineTo(s * .5, s * .59)
            head.lineTo(s * .65, s * .44)
            p.drawPath(head)
            tray = QPainterPath()
            tray.moveTo(s * .23, s * .56)
            tray.lineTo(s * .23, s * .77)
            tray.lineTo(s * .77, s * .77)
            tray.lineTo(s * .77, s * .56)
            p.drawPath(tray)
        elif k == "other":
            knobs = ((.32, .40), (.50, .64), (.68, .34))  # (y, knob x) of each slider
            for y, _x in knobs:
                p.drawLine(QPointF(s * .24, s * y), QPointF(s * .76, s * y))
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            for y, x in knobs:
                p.drawEllipse(QPointF(s * x, s * y), s * .075, s * .075)
        elif k == "download":
            # an open-bottomed cloud with an arrow down out of it, drawn on a
            # 24-unit grid centred in the badge
            u = s * 0.66 / 24
            p.translate(s / 2 - 12 * u, s / 2 - 14.5 * u)
            p.scale(u, u)
            pen.setWidthF(pen.widthF() / u)
            p.setPen(pen)

            def arc(cx, cy, r, start, sweep):  # degrees, counter-clockwise on screen
                cloud.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), start, sweep)

            cloud = QPainterPath()
            cloud.moveTo(20.88, 18.09)
            arc(18, 14, 5, -54.8, 144.8)     # the small bump on the right
            cloud.lineTo(16.74, 9)
            arc(9, 11, 8, 14.5, 206.9)       # the big bump, round to the lower left
            p.drawPath(cloud)
            p.drawLine(QPointF(12, 12), QPointF(12, 21))
            head = QPainterPath()
            head.moveTo(8, 17)
            head.lineTo(12, 21)
            head.lineTo(16, 17)
            p.drawPath(head)


# ---------------------------------------------------------------------------
# Checkbox, segmented control, spinner, progress bar
# ---------------------------------------------------------------------------

class RoundCheck(QAbstractButton):
    """Rounded square checkbox with a drawn check mark."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(24, 24)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = palette()
        r = QRectF(2.5, 2.5, self.width() - 5, self.height() - 5)
        if self.isChecked():
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, QColor(pal.accent))
            g.setColorAt(1, QColor(pal.accent2))
            p.setBrush(QBrush(g))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, 6, 6)
            pen = QPen(QColor(pal.on_accent), 2.2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            path = QPainterPath()
            path.moveTo(r.left() + r.width() * .24, r.top() + r.height() * .52)
            path.lineTo(r.left() + r.width() * .43, r.top() + r.height() * .70)
            path.lineTo(r.left() + r.width() * .77, r.top() + r.height() * .32)
            p.drawPath(path)
        else:
            p.setPen(QPen(QColor(pal.muted if self.underMouse() else pal.faint), 1.6))
            p.setBrush(QColor(pal.input_bg))
            p.drawRoundedRect(r, 6, 6)

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()


class SegmentedControl(QWidget):
    """Pill-style option picker (Dark | Light). Emits changed(key).

    v0.17.0: the selected look is a pill painted by the control itself
    (same colours the stylesheet used: btn_hover fill, accent border) that
    slides from the old option to the new one; the buttons themselves are
    transparent and only switch their text colour. The choice is already
    made when the slide starts, so nothing waits for it."""

    changed = Signal(str)

    def __init__(self, options: List[tuple], current: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = {}
        for key, label in options:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setProperty("segment", True)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(key == current)
            b.clicked.connect(lambda _c=False, k=key: self.changed.emit(k))
            b.toggled.connect(lambda on, btn=b: on and self._slide_to(btn))
            self._group.addButton(b)
            self._buttons[key] = b
            lay.addWidget(b)
        self.setObjectName("inputbox")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._pill: Optional[QRectF] = None      # where the pill is drawn now
        self._pill_from = QRectF()
        self._pill_anim = make_value_anim(self, self._pill_step, ANIM_OPEN_MS - 40, motion_curve())
        _watch_theme(self)

    def set_current(self, key: str) -> None:
        if key in self._buttons:
            self._buttons[key].setChecked(True)

    def current(self) -> Optional[str]:
        b = self._group.checkedButton()
        return next((k for k, v in self._buttons.items() if v is b), None)

    def clear_selection(self) -> None:
        """No option chosen, no pill (v0.23.0: Settings' page tabs, when
        the other control's tab is the one showing)."""
        b = self._group.checkedButton()
        if b is None:
            return
        self._group.setExclusive(False)  # an exclusive group won't uncheck its last button
        b.setChecked(False)
        self._group.setExclusive(True)
        self._pill_anim.stop()
        self._pill = None
        self.update()

    # ---- sliding pill ----------------------------------------------------
    def _checked_rect(self) -> Optional[QRectF]:
        b = self._group.checkedButton()
        return QRectF(b.geometry()) if b is not None else None

    def _slide_to(self, _button) -> None:
        if self._pill is None or not self.isVisible() or not animations_on():
            self._pill = None            # paintEvent snaps to the checked button
            self.update()
            return
        self._pill_from = QRectF(self._pill)
        run_value_anim(self._pill_anim, 0.0, 1.0)

    def _pill_step(self, t: float) -> None:
        target = self._checked_rect()
        if target is None:
            return
        a = self._pill_from
        self._pill = QRectF(a.x() + (target.x() - a.x()) * t, a.y() + (target.y() - a.y()) * t,
                            a.width() + (target.width() - a.width()) * t,
                            a.height() + (target.height() - a.height()) * t)
        self.update()

    def hideEvent(self, e):
        self._pill_anim.stop()
        self._pill = None
        super().hideEvent(e)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._pill is None or self._pill_anim.state() != QAbstractAnimation.Running:
            self._pill = self._checked_rect()     # at rest: follow the button (layout changes)
        if self._pill is None:
            return
        pal = palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(pal.accent), 1))
        p.setBrush(QColor(pal.btn_hover))
        p.drawRoundedRect(self._pill.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)


class Spinner(QWidget):
    """Small circular indeterminate progress indicator."""

    def __init__(self, size: int = 18, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def start(self):
        self.show()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 18) % 360
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = palette()
        s = self.width()
        r = QRectF(2, 2, s - 4, s - 4)
        p.setPen(QPen(QColor(pal.input_border), 2.2))
        p.drawEllipse(r)
        pen = QPen(QColor(pal.accent), 2.4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(r, -self._angle * 16, -100 * 16)


class SegmentedProgressBar(QWidget):
    """The Sync Progress bar: one segment per playlist being synced, so
    the bar reads as 'how far along the whole sync is' (wireframe item
    50). value is measured in segments, e.g. 1.4 = first playlist done
    and the second 40% done.

    set_banner('Sync Complete') makes the bar taller and paints the text
    large across it (Checklist item 15); set_banner('') restores the thin
    bar.

    v0.17.6 (Checklist item 57): set_weights() sizes each segment by its
    playlist's track count, so a 10-song playlist next to two 200-song ones
    takes a sliver of the bar. value is still counted in segments (1.4 =
    the first done, the second 40% done); only where that is drawn changes.
    set_segments(n) gives n equal segments, as before."""

    BAR_HEIGHT = 12
    BANNER_HEIGHT = 36
    BANNER_FONT_PX = 19

    def __init__(self, parent=None):
        super().__init__(parent)
        self._segments = 1
        self._weights = [1.0]
        self._value = 0.0
        # v0.17.0: the fill drawn glides to _value (the real value) instead
        # of jumping; a lower value (a new run starting) is shown at once.
        self._shown = 0.0
        self._fill_anim = make_value_anim(self, self._set_shown, ANIM_BAR_MS)
        self._banner = ""
        self.setFixedHeight(self.BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        _watch_theme(self)

    def _set_shown(self, v: float) -> None:
        self._shown = v
        self.update()

    def set_segments(self, n: int) -> None:
        self.set_weights([1.0] * max(1, n))

    def set_weights(self, weights) -> None:
        """One segment per weight, each as wide as its share of the total
        (a weight below 1 counts as 1)."""
        self._weights = [max(1.0, float(w)) for w in weights] or [1.0]
        self._segments = len(self._weights)
        self.update()

    def _fraction(self, value: float) -> float:
        """How much of the bar a value (counted in segments) fills."""
        value = max(0.0, min(float(value), float(self._segments)))
        whole = int(value)
        filled = sum(self._weights[:whole])
        if whole < self._segments:
            filled += self._weights[whole] * (value - whole)
        return filled / sum(self._weights)

    def set_value(self, v: float) -> None:
        self._value = max(0.0, min(float(v), float(self._segments)))
        anim = self._fill_anim
        if anim.state() == QAbstractAnimation.Running and anim.endValue() == self._value:
            return  # already gliding there (progress text updates repeat the value)
        if not ANIMATIONS or self._value < self._shown or not self.isVisible():
            anim.stop()
            self._set_shown(self._value)
            return
        if self._value != self._shown:
            run_value_anim(anim, self._shown, self._value)

    def set_banner(self, text: str) -> None:
        self._banner = text or ""
        self.setFixedHeight(self.BANNER_HEIGHT if self._banner else self.BAR_HEIGHT)
        self.setAccessibleName(self._banner)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = palette()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = r.height() / 2
        track = QPainterPath()
        track.addRoundedRect(r, radius, radius)
        p.fillPath(track, QColor(pal.input_bg))
        p.setPen(QPen(QColor(pal.input_border), 1))
        p.drawPath(track)

        frac = self._fraction(self._shown)
        if frac > 0:
            p.save()
            p.setClipPath(track)
            fill_w = r.width() * frac
            g = QLinearGradient(r.left(), 0, r.right(), 0)
            g.setColorAt(0, QColor(pal.accent))
            g.setColorAt(1, QColor(pal.accent2))
            p.fillRect(QRectF(r.left(), r.top(), fill_w, r.height()), QBrush(g))
            p.restore()

        if self._segments > 1 and not self._banner:
            p.setPen(QPen(QColor(pal.base), 2))
            total = sum(self._weights)
            edge = 0.0
            for w in self._weights[:-1]:
                edge += w
                x = r.left() + r.width() * edge / total
                p.drawLine(QPointF(x, r.top() - 1), QPointF(x, r.bottom() + 1))

        if self._banner:
            f = ui_theme.base_font()
            f.setPixelSize(self.BANNER_FONT_PX)
            f.setBold(True)
            f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
            p.setFont(f)
            # Same text colour as the gradient primary button when the bar
            # is full; the normal text colour if it somehow isn't.
            p.setPen(QColor(pal.on_accent if self._fraction(self._value) >= 0.5 else pal.text))
            p.drawText(r, Qt.AlignCenter, self._banner)


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def make_panel(object_name: str = "panel") -> QFrame:
    f = QFrame()
    f.setObjectName(object_name)
    f.setAttribute(Qt.WA_StyledBackground, True)
    return f


def make_button(text: str, variant: str = "", compact: bool = False,
                large: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    if variant:
        b.setProperty("variant", variant)
    if compact:
        b.setProperty("compact", True)
    if large:
        b.setProperty("large", True)
    return b


def hseparator() -> QFrame:
    f = QFrame()
    f.setObjectName("sep")
    f.setFixedHeight(1)
    return f


# ---------------------------------------------------------------------------
# In-app overlay (v0.16.13)
# ---------------------------------------------------------------------------

class OverlayHost(QWidget):
    """Dims everything in its parent and shows one page in a centred sheet
    about two-thirds of the parent's size. Not a separate window: it is a
    child covering the parent, so it follows the main window's size and
    position. Esc, a click on the dimmed area, or the page's own close
    button (which calls close_overlay) closes it. While it is open, mouse
    clicks and Tab focus stay inside the sheet.

    v0.23.0: a page can ask for a bigger sheet with an
    OVERLAY_SIZE_FRACTION attribute (Settings); the sheet still keeps
    EDGE px inside the window, so it is never larger than the window."""

    closed = Signal()

    SIZE_FRACTION = 2 / 3
    MIN_W, MIN_H = 680, 460      # used when two-thirds would be smaller...
    EDGE = 24                    # ...but always this far inside the window
    RADIUS = 18

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.sheet = make_panel("overlaysheet")
        self.sheet.setParent(self)
        lay = QVBoxLayout(self.sheet)
        lay.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        lay.addWidget(self.pages)
        self._press_outside = False
        self._return_focus = None
        _watch_theme(self)
        parent.installEventFilter(self)
        self.hide()

    # -- pages ----------------------------------------------------------------
    def add_page(self, page: QWidget) -> None:
        self.pages.addWidget(page)

    def is_open(self) -> bool:
        return self.isVisible()

    def current_page(self) -> Optional[QWidget]:
        return self.pages.currentWidget() if self.isVisible() else None

    def open_page(self, page: QWidget) -> None:
        """Show `page` in the sheet. If another page is already open it is
        swapped out (one overlay at a time)."""
        if self.pages.indexOf(page) < 0:
            self.add_page(page)
        self.pages.setCurrentWidget(page)
        self._place_sheet()  # v0.23.0: each page may ask for its own size
        if not self.isVisible():
            fw = QApplication.focusWidget()
            self._return_focus = fw if fw is not None and not self.isAncestorOf(fw) else None
            self.setGeometry(self.parentWidget().rect())
            self._place_sheet()
            self.show()
        self.raise_()
        self.setFocus(Qt.OtherFocusReason)

    def close_overlay(self) -> None:
        if not self.isVisible():
            return
        self.hide()
        w, self._return_focus = self._return_focus, None
        if w is not None:
            try:
                if w.isVisible():
                    w.setFocus(Qt.OtherFocusReason)
            except RuntimeError:  # the widget was deleted while the overlay was open
                pass
        self.closed.emit()

    # -- geometry -----------------------------------------------------------
    def sheet_rect(self) -> QRect:
        w, h = self.width(), self.height()
        frac = getattr(self.pages.currentWidget(), "OVERLAY_SIZE_FRACTION", self.SIZE_FRACTION)
        sw = max(0, min(max(int(w * frac), self.MIN_W), w - 2 * self.EDGE))
        sh = max(0, min(max(int(h * frac), self.MIN_H), h - 2 * self.EDGE))
        return QRect((w - sw) // 2, (h - sh) // 2, sw, sh)

    def _place_sheet(self) -> None:
        self.sheet.setGeometry(self.sheet_rect())

    def eventFilter(self, obj, e):
        if obj is self.parentWidget() and e.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False

    def resizeEvent(self, e):
        self._place_sheet()
        super().resizeEvent(e)

    # -- painting -----------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(palette().scrim))
        # soft shadow just outside the sheet (cheaper than a graphics effect)
        r = QRectF(self.sheet.geometry())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 12))
        for i in range(1, 11):
            p.drawRoundedRect(r.adjusted(-i, -i + 5, i, i + 5), self.RADIUS + i, self.RADIUS + i)

    # -- input --------------------------------------------------------------
    def _outside_sheet(self, e) -> bool:
        return not self.sheet.geometry().contains(e.position().toPoint())

    def mousePressEvent(self, e):
        # presses inside the sheet that no child used also arrive here
        self._press_outside = e.button() == Qt.LeftButton and self._outside_sheet(e)
        e.accept()

    def mouseReleaseEvent(self, e):
        close = self._press_outside and e.button() == Qt.LeftButton and self._outside_sheet(e)
        self._press_outside = False
        e.accept()
        if close:
            self.close_overlay()

    def wheelEvent(self, e):
        e.accept()  # never scroll the page behind

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            e.accept()
            self.close_overlay()
            return
        super().keyPressEvent(e)

    def focusNextPrevChild(self, next_: bool) -> bool:
        """Keep Tab / Shift+Tab inside the sheet (children ask their
        parents, so this is reached from any focused widget in the sheet)."""
        if not self.isVisible():
            return super().focusNextPrevChild(next_)
        start = QApplication.focusWidget() or self
        w = start
        tab = Qt.TabFocus.value
        for _ in range(1000):
            w = w.nextInFocusChain() if next_ else w.previousInFocusChain()
            if w is None or w is start:
                break
            if (self.sheet.isAncestorOf(w) and w.isVisible() and w.isEnabled()
                    and (w.focusPolicy().value & tab)):
                w.setFocus(Qt.TabFocusReason if next_ else Qt.BacktabFocusReason)
                return True
        return True
