"""The Personal Collection list's Card cell: a thumbnail beside the card name.

Mirrors ``_inventory_delegates.CardCellDelegate`` — same 26x36 thumb, same
placeholder when there is no art — so a card looks the same on both screens.
It carries no sub-line, because unlike Inventory this table already has Set,
No. and Printing as their own sortable columns; repeating them under the name
would print the row twice.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QStyledItemDelegate

from tcginventory.services.collections import MISSING
from tcginventory.ui import card_thumbs, theme
from tcginventory.ui.models.collection_model import SLOT_ROLE

# Matches _inventory_delegates so the two tables line up visually.
_THUMB_W, _THUMB_H = 26, 36
_ROW_H = 44


class SlotCardDelegate(QStyledItemDelegate):
    """Thumbnail + card name. Unowned slots are drawn dimmed, not hidden."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 (Qt override)
        return QSize(200, _ROW_H)

    def paint(self, painter: QPainter, option, index) -> None:
        t = theme.active()
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            slot = index.data(SLOT_ROLE)
            rect = option.rect

            x = rect.left() + 8
            y = rect.top() + (rect.height() - _THUMB_H) // 2
            pixmap = (
                card_thumbs.thumbnail(slot.card_id, _THUMB_W)
                if slot is not None
                else None
            )
            if pixmap is not None and not pixmap.isNull():
                path = QPainterPath()
                path.addRoundedRect(x, y, _THUMB_W, _THUMB_H, 3, 3)
                painter.save()
                painter.setClipPath(path)
                painter.drawPixmap(x, y, pixmap)
                painter.restore()
            else:
                # Same placeholder as the Inventory grid: a card-shaped blank
                # rather than an empty gap, so rows stay aligned.
                path = QPainterPath()
                path.addRoundedRect(x, y, _THUMB_W, _THUMB_H, 3, 3)
                painter.fillPath(path, QColor(t.surface2))

            text_x = x + _THUMB_W + 10
            font = QFont(option.font)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            name = (slot.name if slot is not None else "") or ""
            owned = slot is not None and slot.state != MISSING
            painter.setPen(QColor(t.ink if owned else t.ink3))
            painter.drawText(
                text_x,
                rect.top() + rect.height() // 2 + metrics.ascent() // 2 - 1,
                metrics.elidedText(
                    name, Qt.ElideRight, max(20, rect.right() - 8 - text_x)
                ),
            )
        finally:
            painter.restore()
