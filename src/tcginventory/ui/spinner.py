"""A small themed busy indicator: a spinning arc, and a labelled chip around it.

The app's existing "busy" convention is a 4 px indeterminate rail along a tile
(``opBar`` in the Operations window), which suits a tile with one job. A
collection warming its card art needs something inline in a header that can also
carry a count, so this pairs the same idea with a glyph and a sentence.

Both stop their timer when hidden. A ``QTimer`` left running on a hidden widget
repaints forever, which on a screen the user has navigated away from is pure
battery.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from tcginventory.ui import theme

# 12 steps at 60 ms is a 0.72 s revolution — brisk enough to read as working,
# slow enough not to strobe.
_STEPS = 12
_INTERVAL_MS = 60


class Spinner(QWidget):
    """An arc that rotates while visible. Nothing else: no text, no layout."""

    def __init__(self, diameter: int = 14, parent=None) -> None:
        super().__init__(parent)
        self._step = 0
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)
        self._timer = QTimer(self)
        self._timer.setInterval(_INTERVAL_MS)
        self._timer.timeout.connect(self._advance)

    def _advance(self) -> None:
        self._step = (self._step + 1) % _STEPS
        self.update()

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self.start()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(event)
        self.stop()  # a hidden spinner must not keep repainting

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        t = theme.active()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            inset = 2.0
            box = QRectF(
                inset,
                inset,
                self._diameter - 2 * inset,
                self._diameter - 2 * inset,
            )
            # A faint full ring, with a brighter accent arc riding around it, so
            # the shape reads as a circle even at 14 px rather than as a comet.
            track = QColor(t.line2)
            painter.setPen(QPen(track, 2.0, Qt.SolidLine, Qt.RoundCap))
            painter.drawArc(box, 0, 360 * 16)
            painter.setPen(QPen(QColor(t.accent), 2.0, Qt.SolidLine, Qt.RoundCap))
            start = 90 * 16 - int(self._step / _STEPS * 360 * 16)
            painter.drawArc(box, start, -100 * 16)
        finally:
            painter.end()  # an unended painter is its own crash


class BusyChip(QWidget):
    """A spinner plus a line of text, hidden unless something is happening.

    ``begin`` may be called before the work is known to be slow: pass
    ``delay_ms`` and the chip only appears if the work is still going then, so a
    warm that finishes in 300 ms never flashes a spinner at anyone.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("busyChip")
        # A plain QWidget does not paint its stylesheet background or border
        # without this — the pill was invisible and only the spinner and text
        # showed. QFrame subclasses get it for free; QWidget does not.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._spinner = Spinner(14, self)
        self._label = QLabel()
        self._label.setObjectName("busyChipText")
        row = QHBoxLayout(self)
        row.setContentsMargins(9, 4, 11, 4)
        row.setSpacing(7)
        row.addWidget(self._spinner)
        row.addWidget(self._label)
        self._reveal = QTimer(self)
        self._reveal.setSingleShot(True)
        self._reveal.timeout.connect(self._show_now)
        self._pending_text = ""
        self.setVisible(False)
        self.setStyleSheet(_qss(theme.active()))

    def begin(self, text: str, delay_ms: int = 0) -> None:
        self._pending_text = text
        self._label.setText(text)
        if delay_ms <= 0:
            self._show_now()
        else:
            self._reveal.start(delay_ms)

    def update_text(self, text: str) -> None:
        self._pending_text = text
        self._label.setText(text)

    def end(self) -> None:
        self._reveal.stop()
        self.setVisible(False)
        self._spinner.stop()

    def _show_now(self) -> None:
        self._label.setText(self._pending_text)
        self.setVisible(True)
        self._spinner.start()


def _qss(t) -> str:
    return f"""
    QWidget#busyChip {{ background:{t.surface2}; border:1px solid {t.line2};
        border-radius:13px; }}
    QLabel#busyChipText {{ color:{t.ink2}; font-size:11.5px; }}
    """
