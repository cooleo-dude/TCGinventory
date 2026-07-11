"""Delegate for the 'Market Price Current' column.

Review-marked cells get an amber background, an inline price editor (double-click
to type a value), and a **two-stage** button: the first click "arms" it (previews
the unmark — highlight off, label becomes Refresh); the second click commits
(unmark + refresh). It reverts to stage one if the selection moves elsewhere or
the page changes.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDoubleSpinBox, QStyle, QStyledItemDelegate

from tcginventory.ui.models.inventory_model import ITEM_ID_ROLE, REVIEW_ROLE

_BTN_W = 84
_AMBER = QColor("#fff3cd")
_BTN_BG = QColor("#c8912a")


def _button_rect(cell: QRect) -> QRect:
    return QRect(cell.right() - _BTN_W - 3, cell.top() + 3, _BTN_W, cell.height() - 6)


class ReviewPriceDelegate(QStyledItemDelegate):
    def __init__(self, view, on_commit) -> None:
        super().__init__(view)
        self._view = view
        self._on_commit = on_commit  # (item_id) -> None : unmark + refresh
        self._armed_item_id: int | None = None

    def disarm(self) -> None:
        if self._armed_item_id is not None:
            self._armed_item_id = None
            self._view.viewport().update()

    def note_selected_item(self, item_id: int | None) -> None:
        """Revert to stage one when a different card is selected."""
        if self._armed_item_id is not None and self._armed_item_id != item_id:
            self.disarm()

    # -- painting -----------------------------------------------------------

    def paint(self, painter, option, index) -> None:
        if not index.data(REVIEW_ROLE):
            super().paint(painter, option, index)
            return
        armed = index.data(ITEM_ID_ROLE) == self._armed_item_id
        painter.save()
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
            painter.setPen(option.palette.highlightedText().color())
        else:
            if not armed:  # armed = preview-unmarked → no highlight
                painter.fillRect(option.rect, _AMBER)
            painter.setPen(option.palette.text().color())

        text = index.data(Qt.DisplayRole) or ""
        text_rect = option.rect.adjusted(4, 0, -_BTN_W - 8, 0)
        painter.drawText(text_rect, int(Qt.AlignRight | Qt.AlignVCenter), text)

        btn = _button_rect(option.rect)
        painter.fillRect(btn, _BTN_BG)
        painter.setPen(QColor("#ffffff"))
        label = "Refresh ↻" if armed else "Unmark ✕"
        painter.drawText(btn, int(Qt.AlignCenter), label)
        painter.restore()

    # -- button interaction -------------------------------------------------

    def editorEvent(self, event, model, option, index) -> bool:
        if index.data(REVIEW_ROLE) and event.type() == QEvent.MouseButtonRelease:
            if _button_rect(option.rect).contains(event.pos()):
                item_id = index.data(ITEM_ID_ROLE)
                if self._armed_item_id == item_id:  # second click → commit
                    self._armed_item_id = None
                    self._on_commit(item_id)
                else:  # first click → arm
                    self._armed_item_id = item_id
                    self._view.viewport().update()
                return True
        return super().editorEvent(event, model, option, index)

    # -- inline price editor (double-click) ---------------------------------

    def createEditor(self, parent, option, index):
        spin = QDoubleSpinBox(parent)
        spin.setPrefix("$")
        spin.setDecimals(2)
        spin.setMaximum(10_000_000.0)
        spin.setGroupSeparatorShown(True)
        return spin

    def setEditorData(self, editor, index) -> None:
        editor.setValue(float(index.data(Qt.EditRole) or 0.0))

    def setModelData(self, editor, model, index) -> None:
        editor.interpretText()
        model.setData(index, editor.value(), Qt.EditRole)
