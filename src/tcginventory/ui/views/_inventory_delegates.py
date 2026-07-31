"""Stylized Inventory-grid delegates: the composite Card cell (art + name + sold
tag + sub-line), the condition/grade badge, and the intent chip.

They paint against the Ember Duo theme and draw their own row background so the
custom cells match the view's selection / alternate-row / hover states.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from tcginventory.ui import theme
from tcginventory.ui.models.inventory_model import INVROW_ROLE, subline_for
from tcginventory.ui.views._combo_delegate import ComboDelegate
from tcginventory.ui.views._intent_delegate import IntentDelegate

_THUMB_W, _THUMB_H = 26, 36
_ROW_H = 46


def _row_bg(painter: QPainter, option, t, row: int) -> None:
    """Fill the standard row background (base / zebra / hover / selection) so a
    custom-painted cell matches the plainly-painted columns beside it. ``row`` is
    the view row index — the view alternates on the same parity, so deriving the
    zebra from it keeps custom cells aligned with the native ones."""
    alt = row % 2 == 1
    painter.fillRect(option.rect, QColor(t.zebra if alt else t.surface))
    if option.state & QStyle.State_Selected:
        wash = QColor(t.accent)
        wash.setAlpha(38)  # ≈ accent_soft (0.15) — matches native cell selection
        painter.fillRect(option.rect, wash)
        painter.fillRect(
            QRect(option.rect.left(), option.rect.top(), 2, option.rect.height()),
            QColor(t.accent),
        )
    elif option.state & QStyle.State_MouseOver:
        painter.fillRect(option.rect, QColor(t.surface2))


def _pill(
    painter: QPainter, x: int, cy: int, text: str, fg: str, bg: QColor, font: QFont
) -> int:
    """Draw a rounded pill centred vertically at ``cy``, starting at ``x``.
    Returns its right edge."""
    painter.setFont(font)
    fm = painter.fontMetrics()
    w = fm.horizontalAdvance(text) + 16
    h = 18
    y = cy - h // 2
    path = QPainterPath()
    path.addRoundedRect(x, y, w, h, 8, 8)
    painter.fillPath(path, bg)
    painter.setPen(QColor(fg))
    painter.drawText(QRect(x, y, w, h), int(Qt.AlignCenter), text)
    return x + w


def _soft(hex_color: str, alpha: int = 40) -> QColor:
    c = QColor(hex_color)
    c.setAlpha(alpha)
    return c


class CardCellDelegate(QStyledItemDelegate):
    """The Card column: thumbnail + card name + Sold/Partial tag + sub-line."""

    def __init__(self, view, thumb_provider) -> None:
        super().__init__(view)
        self._thumb = thumb_provider  # (lot_id) -> QPixmap | None

    def sizeHint(self, option, index) -> QSize:
        return QSize(240, _ROW_H)

    def paint(self, painter, option, index) -> None:
        t = theme.active()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        _row_bg(painter, option, t, index.row())
        row = index.data(INVROW_ROLE)
        r = option.rect

        tx = r.left() + 10
        ty = r.top() + (r.height() - _THUMB_H) // 2
        pm = self._thumb(row.lot_id) if row is not None else None
        if pm is not None and not pm.isNull():
            painter.drawPixmap(tx, ty, pm)
        else:
            path = QPainterPath()
            path.addRoundedRect(tx, ty, _THUMB_W, _THUMB_H, 4, 4)
            painter.fillPath(path, QColor(t.surface2))

        text_x = tx + _THUMB_W + 11
        right = r.right() - 8
        name = row.item.name if row is not None else ""

        name_font = QFont(option.font)
        name_font.setBold(True)
        painter.setFont(name_font)
        fm = painter.fontMetrics()
        name_cy = r.top() + r.height() // 2 - fm.height() // 2 + fm.ascent()

        # Sold / Partial tag, right-aligned on the name line.
        tag_left = right
        status = row.sold_status if row is not None else "unsold"
        if status in ("sold", "partial"):
            label = "Sold" if status == "sold" else "Partial"
            tag_fg = t.ink2 if status == "sold" else t.warn
            tag_bg = _soft(t.ink3, 55) if status == "sold" else _soft(t.warn, 40)
            small = QFont(option.font)
            small.setPointSizeF(max(7.0, option.font.pointSizeF() - 2))
            small.setBold(True)
            tfm_w = self._pill_width(painter, label, small)
            tag_left = right - tfm_w
            _pill(
                painter,
                tag_left,
                r.top() + r.height() // 2,
                label,
                tag_fg,
                tag_bg,
                small,
            )

        painter.setFont(name_font)
        painter.setPen(QColor(t.ink))
        name_avail = max(20, tag_left - 8 - text_x)
        painter.drawText(
            text_x, name_cy, fm.elidedText(name, Qt.ElideRight, name_avail)
        )

        sub_font = QFont(option.font)
        sub_font.setPointSizeF(max(7.5, option.font.pointSizeF() - 1.5))
        painter.setFont(sub_font)
        painter.setPen(QColor(t.ink3))
        sfm = painter.fontMetrics()
        sub = subline_for(row) if row is not None else ""
        painter.drawText(
            text_x,
            name_cy + sfm.height(),
            sfm.elidedText(sub, Qt.ElideRight, right - text_x),
        )
        painter.restore()

    @staticmethod
    def _pill_width(painter, text, font) -> int:
        painter.setFont(font)
        return painter.fontMetrics().horizontalAdvance(text) + 16


class BadgeDelegate(ComboDelegate):
    """Condition / grade as a badge (graded slabs tinted accent). Keeps the
    inline drop-down editor from :class:`ComboDelegate`."""

    def paint(self, painter, option, index) -> None:
        t = theme.active()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        _row_bg(painter, option, t, index.row())
        text = index.data(Qt.DisplayRole) or ""
        if text:
            row = index.data(INVROW_ROLE)
            graded = bool(row.item.is_graded) if row is not None else False
            fg = t.accent_ink if graded else t.ink2
            bg = _soft(t.accent, 36) if graded else QColor(t.surface2)
            font = QFont(option.font)
            font.setBold(graded)
            _pill(
                painter,
                option.rect.left() + 8,
                option.rect.center().y() + 1,
                text,
                fg,
                bg,
                font,
            )
        painter.restore()


class IntentChipDelegate(IntentDelegate):
    """Intent as a coloured chip (resale=accent, personal=blue, ripping=amber).
    Keeps the inline drop-down editor from :class:`IntentDelegate`."""

    def paint(self, painter, option, index) -> None:
        t = theme.active()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        _row_bg(painter, option, t, index.row())
        val = index.data(Qt.DisplayRole) or ""
        if val:
            color = {
                "resale": t.accent_ink,
                "personal": t.info,
                "ripping": t.warn,
                "mixed": t.ink2,
            }.get(val, t.ink2)
            font = QFont(option.font)
            font.setBold(True)
            _pill(
                painter,
                option.rect.left() + 8,
                option.rect.center().y() + 1,
                val,
                color,
                _soft(color, 38),
                font,
            )
        painter.restore()
