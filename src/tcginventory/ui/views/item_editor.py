"""Item editor dialog — maintain an existing item (F-IT-3/4/5/6).

Items are created automatically by transactions; this only *edits* them: fix
identity typos, refresh market value/links, set SKU/location/notes. Delete is
guarded — blocked while any lot or line references the item (use an
adjustment-out instead), matching the ratified soft-delete rule.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import (
    ItemRepository,
    LotRepository,
    TransactionLineRepository,
)


class ItemEditorDialog(QDialog):
    def __init__(self, conn: sqlite3.Connection, item_id: int, parent=None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._items = ItemRepository(conn)
        self._item = self._items.get(item_id)
        self.setWindowTitle(f"Edit — {self._item.name if self._item else 'item'}")

        self._name = QLineEdit(self._item.name if self._item else "")
        self._set_name = QLineEdit(self._item.set_name or "" if self._item else "")
        self._card_number = QLineEdit(
            self._item.card_number or "" if self._item else ""
        )

        self._market = QDoubleSpinBox()
        self._market.setPrefix("$")
        self._market.setDecimals(2)
        self._market.setMaximum(10_000_000.0)
        self._market.setGroupSeparatorShown(True)
        if self._item and self._item.market_value_cents is not None:
            self._market.setValue(self._item.market_value_cents / 100)

        self._tcgplayer = QLineEdit(
            self._item.tcgplayer_url or "" if self._item else ""
        )
        self._ebay = QLineEdit(self._item.ebay_url or "" if self._item else "")
        self._notes = QLineEdit(self._item.notes or "" if self._item else "")

        form = QFormLayout(self)
        form.addRow("Name:", self._name)
        form.addRow("Set:", self._set_name)
        form.addRow("Card #:", self._card_number)
        form.addRow("Market value:", self._market)
        form.addRow("TCGplayer URL:", self._tcgplayer)
        form.addRow("eBay URL:", self._ebay)
        form.addRow("Notes:", self._notes)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        delete = QPushButton("Delete…")
        delete.clicked.connect(self._delete)
        buttons.addButton(delete, QDialogButtonBox.DestructiveRole)
        form.addRow(buttons)

    def _save(self) -> None:
        if self._item is None:
            self.reject()
            return
        self._item.name = self._name.text().strip() or self._item.name
        self._item.set_name = self._set_name.text().strip() or None
        self._item.card_number = self._card_number.text().strip() or None
        self._item.market_value_cents = round(self._market.value() * 100)
        self._item.market_value_asof = now_iso()[:10]
        self._item.tcgplayer_url = self._tcgplayer.text().strip() or None
        self._item.ebay_url = self._ebay.text().strip() or None
        self._item.notes = self._notes.text().strip() or None
        self._items.update(self._item)
        self._conn.commit()
        self.accept()

    def _delete(self) -> None:
        if self._item is None:
            return
        lots = LotRepository(self._conn).list_for_item(self._item.id)
        lines = TransactionLineRepository(self._conn).list_for_item(self._item.id)
        if lots or lines:
            QMessageBox.warning(
                self,
                "Cannot delete",
                "This item has transaction history. Use an adjustment-out to "
                "remove remaining units instead of deleting the item.",
            )
            return
        if (
            QMessageBox.question(self, "Delete item", f"Delete {self._item.name}?")
            == QMessageBox.Yes
        ):
            self._items.delete(self._item.id)
            self._conn.commit()
            self.accept()
