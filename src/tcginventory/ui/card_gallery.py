"""Reusable card display: one focused card, or a wrapping gallery of cards.

Separates *building* the gallery (``set_cards`` — rebuild thumbnails only when the
set changes, so their images load once and persist) from *showing* it
(``show_gallery`` / ``show_single`` — just flip between the two, cheap). Each
gallery thumbnail can carry a coloured border (e.g. to match a chart line).
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QLayout,
    QScrollArea,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from tcginventory.ui.card_preview import CardImagePane


class FlowLayout(QLayout):
    """Left-to-right layout that wraps to a new row when it runs out of width."""

    def __init__(self, parent=None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            nx = x + hint.width() + self._spacing
            if nx - self._spacing > rect.right() and line_h > 0:
                x = rect.x()
                y += line_h + self._spacing
                nx = x + hint.width() + self._spacing
                line_h = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class CardGallery(QWidget):
    """Shows a single focused card, or a wrapping gallery of bordered cards.

    Callers drive the two modes: ``show_single`` for a focused card and
    ``show_gallery`` for the set. ``set_cards`` (re)builds the gallery contents —
    call it only when the set changes, not on every mode switch, so thumbnail
    images load once and persist.
    """

    _THUMB = QSize(150, 210)
    _EMPTY_HINT = "Nothing to show yet."

    def __init__(self, empty_hint: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self._empty_hint = empty_hint or self._EMPTY_HINT
        self._single = CardImagePane()
        self._gallery_widget = QWidget()
        self._gallery = FlowLayout(self._gallery_widget)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._gallery_widget)
        self._stack = QStackedLayout(self)
        self._stack.addWidget(self._single)  # 0 — focused card
        self._stack.addWidget(self._scroll)  # 1 — gallery
        self.set_cards([])

    def show_single(self, card_id, url) -> None:
        self._single.show_card(card_id, url)
        self._stack.setCurrentIndex(0)

    def show_gallery(self) -> None:
        self._stack.setCurrentIndex(1)

    def set_cards(self, entries: list[tuple[str, str, str]]) -> None:
        """Rebuild the gallery. ``entries`` = ``(card_id, image_url, border)``."""
        while self._gallery.count():
            item = self._gallery.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Detach synchronously: deleteLater() is deferred and isn't
                # delivered by processEvents(), so on rapid rebuilds the old
                # thumbnails would linger — still parented and visible, piling
                # up at (0, 0) on top of the new ones (only the last shows).
                widget.setParent(None)
                widget.deleteLater()
        if not entries:
            hint = QLabel(self._empty_hint)
            hint.setWordWrap(True)
            hint.setStyleSheet("color:#a89984;")
            self._gallery.addWidget(hint)
        else:
            for card_id, url, color in entries:
                self._gallery.addWidget(self._thumb(card_id, url, color))

    def _thumb(self, card_id, url, color: str) -> QFrame:
        frame = QFrame()
        frame.setFixedSize(self._THUMB)
        frame.setStyleSheet(
            f"QFrame {{ border: 3px solid {color}; border-radius: 6px; "
            "background: #1d2021; }"
        )
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(4, 4, 4, 4)
        pane = CardImagePane()
        pane.set_border("#1d2021", width=0)  # the frame provides the coloured edge
        pane.show_card(card_id, url)
        inner.addWidget(pane)
        return frame
