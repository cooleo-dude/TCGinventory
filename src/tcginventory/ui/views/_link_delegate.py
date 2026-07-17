"""Delegate that paints a small 'open URL' push button in a table cell.

Used by the Inventory links panel for the TCGplayer / eBay columns. The button
is enabled only when the row actually has that link (read from
:data:`LINK_URL_ROLE`); the click itself is handled by the view's ``clicked``
signal, which opens the URL.
"""

from __future__ import annotations

from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionButton

from tcginventory.ui.models.inventory_model import LINK_URL_ROLE


class LinkButtonDelegate(QStyledItemDelegate):
    def __init__(self, view, label: str) -> None:
        super().__init__(view)
        self._view = view
        self._label = label

    def paint(self, painter, option, index) -> None:
        has_url = bool(index.data(LINK_URL_ROLE))
        opt = QStyleOptionButton()
        opt.rect = option.rect.adjusted(2, 2, -2, -2)
        opt.text = self._label if has_url else "—"
        opt.state = QStyle.State_Enabled if has_url else QStyle.State_None
        if has_url and option.state & QStyle.State_MouseOver:
            opt.state |= QStyle.State_MouseOver
        self._view.style().drawControl(QStyle.CE_PushButton, opt, painter)
