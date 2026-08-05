"""The startup screen shown while the app assembles itself.

Opening the ledger and taking the startup backup are quick (~110 ms together);
building the ten workspace views is not — about two seconds on a real collection,
spread across them (Inventory ~420 ms, Operations ~300 ms, Market ~180 ms, and so
on). That used to be two seconds of nothing at all.

So this screen goes up as soon as Qt exists and names each piece as it lands.
:meth:`StartupSplash.step` pumps the event loop, which is what lets the mascot
animate and the bar advance *between* views — no view can be built off the main
thread, but none of them blocks long enough to look stuck.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QMovie
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from tcginventory import __version__
from tcginventory.ui import theme
from tcginventory.ui.assets import scrafty_movie_path

_MASCOT_H = 128  # tall enough to read as the app's mascot, not an icon
_MASCOT_GROUND = QColor(105, 105, 105)  # the GIF's flat grey ground, keyed out


class StartupSplash(QWidget):
    """Frameless, centred, Ember Duo — the app's face for its first two seconds.

    ``total_steps`` sizes the bar so it fills honestly rather than crawling to an
    arbitrary 90%; extra steps clamp instead of overflowing.
    """

    def __init__(self, total_steps: int, parent=None) -> None:
        super().__init__(parent)
        self._total = max(1, total_steps)
        self._done = 0
        t = theme.active()

        self.setWindowFlags(
            Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)  # so the corners round
        self.setWindowTitle(f"Starting {__version__}")

        card = QFrame()
        card.setObjectName("splashCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)  # room for the shadow
        outer.addWidget(card)

        row = QHBoxLayout(card)
        row.setContentsMargins(22, 20, 24, 20)
        row.setSpacing(20)

        self._mascot = QLabel()
        self._mascot.setFixedSize(int(_MASCOT_H * 0.86), _MASCOT_H)
        self._mascot.setAlignment(Qt.AlignCenter)
        self._movie = QMovie(scrafty_movie_path())
        if self._movie.isValid():
            self._movie.jumpToFrame(0)
            native = self._movie.currentPixmap().size()
            if native.width():
                aspect = native.height() / native.width()
                self._movie.setScaledSize(
                    QSize(int(round(_MASCOT_H / aspect)), _MASCOT_H)
                )
            # The GIF has an opaque grey ground rather than transparency, which
            # would sit on the dark card as a flat rectangle. Key it out frame by
            # frame so the mascot is a cutout.
            self._movie.frameChanged.connect(self._show_frame)
            self._movie.start()
        row.addWidget(self._mascot, 0, Qt.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(3)
        title = QLabel("TCGinventory")
        title.setObjectName("splashTitle")
        text.addWidget(title)
        sub = QLabel("Personal and business card ledger")
        sub.setObjectName("splashSub")
        text.addWidget(sub)
        text.addSpacing(10)

        self._status = QLabel("Starting…")
        self._status.setObjectName("splashStatus")
        self._status.setFont(self._mono(10.5))
        text.addWidget(self._status)

        self._bar = QProgressBar()
        self._bar.setObjectName("splashBar")
        self._bar.setRange(0, self._total)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(4)
        text.addWidget(self._bar)

        version = QLabel(f"v{__version__}")
        version.setObjectName("splashVersion")
        version.setFont(self._mono(9.5))
        version.setAlignment(Qt.AlignRight)
        text.addSpacing(2)
        text.addWidget(version)
        row.addLayout(text, 1)

        self.setStyleSheet(_qss(t))
        self.setFixedSize(self.sizeHint().expandedTo(self.minimumSizeHint()))
        self.resize(496, 208)

    @staticmethod
    def _mono(size: float) -> QFont:
        font = QFont(theme.mono_family())
        font.setPointSizeF(size)
        return font

    def _show_frame(self, _index: int = 0) -> None:
        """Paint the current mascot frame with its grey ground masked away."""
        pixmap = self._movie.currentPixmap()
        if pixmap.isNull():
            return
        mask = pixmap.createMaskFromColor(_MASCOT_GROUND, Qt.MaskInColor)
        pixmap.setMask(mask)
        self._mascot.setPixmap(pixmap)

    # -- lifecycle ----------------------------------------------------------

    def begin(self, label: str = "Starting…") -> None:
        """Show, centre on the active screen, and paint before any work starts."""
        self._status.setText(label)
        screen = QApplication.screenAt(self.pos()) or QApplication.primaryScreen()
        if screen is not None:
            centre = screen.availableGeometry().center()
            self.move(centre.x() - self.width() // 2, centre.y() - self.height() // 2)
        self.show()
        self.raise_()
        self._pump()

    def step(self, label: str) -> None:
        """Name what is being built now, advance the bar, and let Qt breathe.

        The pump is the point: every view is built on this thread, so without it
        nothing would repaint and the mascot would sit frozen until the window
        appeared.
        """
        self._done = min(self._done + 1, self._total)
        self._status.setText(label)
        self._bar.setValue(self._done)
        self._pump()

    def finish(self, window: QWidget | None = None) -> None:
        """Hand over to the real window and get out of the way."""
        self._bar.setValue(self._total)
        self._status.setText("Ready")
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()
        if self._movie.isValid():
            self._movie.stop()
        self.close()

    def _pump(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()


def _qss(t: theme.Tokens) -> str:
    return f"""
    #splashCard {{
        background: {t.surface};
        border: 1px solid {t.line2};
        border-top: 3px solid {t.accent};
        border-radius: 16px;
    }}
    #splashTitle {{ color: {t.ink}; font-size: 22px; font-weight: 800; }}
    #splashSub {{ color: {t.ink3}; font-size: 12px; }}
    #splashStatus {{ color: {t.accent_ink}; }}
    #splashVersion {{ color: {t.ink3}; }}
    #splashBar {{ background: {t.line}; border: none; border-radius: 2px; }}
    #splashBar::chunk {{ background: {t.accent}; border-radius: 2px; }}
    """
