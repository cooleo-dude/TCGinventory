"""A page selector: ``‹ 1 … 6 7 8 … 116 ›``.

Extracted from the eBay Auctions watchlist, which built one button per page
inline. That is fine for a watchlist of three pages and wrong at scale: a
2,072-slot Personal Collection at 18 cells a page is **116 pages**, and 116 dots
is not a control. So the button row is *windowed* — first page, last page, a few
either side of the current one, and an ellipsis across each gap — and lives here
so there is one pager rather than one per screen. (Ticket 64.)
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from tcginventory.ui import theme

# Pages either side of the current one that always get their own button. Two
# keeps the row a stable width — at most 1 … 3 4 5 6 7 … 116, nine controls —
# while still letting you step to a neighbour without using the arrows.
_WINDOW = 2


def page_numbers(current: int, pages: int, window: int = _WINDOW) -> list[int | None]:
    """The buttons to draw, ``None`` standing for an ellipsis.

    Pure, so the windowing is testable without a widget::

        page_numbers(0, 3)    -> [0, 1, 2]
        page_numbers(60, 116) -> [0, None, 58, 59, 60, 61, 62, None, 115]

    An ellipsis is only used where it actually saves buttons: a gap of exactly
    one page shows that page instead, because "… 4 …" is longer than "4".
    """
    if pages <= 0:
        return []
    wanted = {0, pages - 1}
    wanted.update(
        p for p in range(current - window, current + window + 1) if 0 <= p < pages
    )
    out: list[int | None] = []
    previous: int | None = None
    for page in sorted(wanted):
        if previous is not None and page - previous > 1:
            if page - previous == 2:
                out.append(previous + 1)  # one page gap — show it, don't hide it
            else:
                out.append(None)
        out.append(page)
        previous = page
    return out


class Pager(QWidget):
    """Page buttons with previous/next arrows. Emits 0-based page indexes."""

    page_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._page = 0
        self._pages = 1
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 4, 0, 2)
        self._row.setSpacing(6)

    # -- state --------------------------------------------------------------

    @property
    def page(self) -> int:
        return self._page

    @property
    def pages(self) -> int:
        return self._pages

    def set_pages(self, pages: int, page: int | None = None) -> None:
        """Set the page count, clamping the current page into range.

        Called after a filter changes the row count: a page 9 that no longer
        exists lands on the new last page rather than showing an empty grid.
        """
        self._pages = max(1, pages)
        self._page = max(0, min(self._page if page is None else page, self._pages - 1))
        self._rebuild()

    def set_page(self, page: int) -> None:
        target = max(0, min(page, self._pages - 1))
        if target == self._page:
            return
        self._page = target
        self._rebuild()
        self.page_changed.emit(self._page)

    # -- drawing ------------------------------------------------------------

    def _rebuild(self) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) *before* deleteLater: deletion is deferred to
                # the event loop, so on a control that rebuilds on every page
                # change the old buttons would stay parented and visible in the
                # meantime and pile up. (ui/card_gallery.py hit the same thing.)
                widget.setParent(None)
                widget.deleteLater()
        # One page is not a choice; hide the control rather than show "1".
        self.setVisible(self._pages > 1)
        if self._pages <= 1:
            return
        self._row.addStretch(1)
        prev = self._arrow("‹", self._page > 0)
        prev.clicked.connect(lambda: self.set_page(self._page - 1))
        self._row.addWidget(prev)
        for entry in page_numbers(self._page, self._pages):
            if entry is None:
                self._row.addWidget(self._ellipsis())
            else:
                self._row.addWidget(self._dot(entry))
        nxt = self._arrow("›", self._page < self._pages - 1)
        nxt.clicked.connect(lambda: self.set_page(self._page + 1))
        self._row.addWidget(nxt)
        self._row.addStretch(1)

    def _dot(self, page: int) -> QPushButton:
        t = theme.active()
        on = page == self._page
        button = QPushButton(str(page + 1))
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedSize(max(26, 12 + 8 * len(str(page + 1))), 26)
        button.setStyleSheet(
            # padding:0 is load-bearing. The app-level stylesheet gives every
            # QPushButton `padding: 6px 14px`, and these are fixed at 26px wide,
            # so 28px of horizontal padding left a negative content width: Qt
            # elided the digit to nothing and the pager drew as empty boxes. The
            # inline version this was extracted from had the same bug.
            f"QPushButton{{border:1px solid {t.accent if on else t.line2};"
            f"border-radius:7px;padding:0;"
            f"background:{t.accent_soft if on else 'transparent'};"
            f"color:{t.accent_ink if on else t.ink2};font-size:11px;"
            f"font-weight:700;}}"
            f"QPushButton:hover{{border-color:{t.accent};}}"
        )
        button.clicked.connect(lambda _=False, p=page: self.set_page(p))
        return button

    def _ellipsis(self) -> QLabel:
        t = theme.active()
        label = QLabel("…")
        label.setAlignment(Qt.AlignCenter)
        label.setFixedSize(18, 26)
        label.setStyleSheet(f"color:{t.ink3};font-size:13px;")
        return label

    def _arrow(self, glyph: str, enabled: bool) -> QPushButton:
        t = theme.active()
        button = QPushButton(glyph)
        button.setFixedSize(28, 26)
        button.setEnabled(enabled)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet(
            f"QPushButton{{border:none;background:transparent;padding:0;"
            f"color:{t.ink2};font-size:17px;font-weight:700;}}"
            f"QPushButton:hover{{color:{t.accent_ink};}}"
            f"QPushButton:disabled{{color:{t.ink3};}}"
        )
        return button
