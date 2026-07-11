"""Item detail dialog — attributes, open lots, and transaction history (F-VW-2)."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from tcginventory.data.repositories import (
    ItemRepository,
    LotRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.services.inventory import InventoryService
from tcginventory.ui.formatting import money


def _readonly_item(text: str, numeric: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    if numeric:
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return item


class ItemDetailDialog(QDialog):
    def __init__(self, conn: sqlite3.Connection, item_id: int, parent=None) -> None:
        super().__init__(parent)
        self._conn = conn
        item = ItemRepository(conn).get(item_id)
        title = item.name if item else "Item"
        self.setWindowTitle(f"{title} — detail")
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        layout.addWidget(self._header(item))
        layout.addWidget(self._position_line(item_id))

        layout.addWidget(QLabel("<b>Lots</b>"))
        layout.addWidget(self._lots_table(item_id))

        layout.addWidget(QLabel("<b>History</b>"))
        layout.addWidget(self._history_table(item_id))

        if item is not None:
            buttons = QHBoxLayout()
            buttons.addStretch(1)
            edit = QPushButton("Edit item…")
            edit.clicked.connect(lambda: self._edit_item(item_id))
            buttons.addWidget(edit)
            close = QPushButton("Close")
            close.clicked.connect(self.reject)
            buttons.addWidget(close)
            layout.addLayout(buttons)

    def _edit_item(self, item_id: int) -> None:
        from tcginventory.ui.views.item_editor import ItemEditorDialog

        if ItemEditorDialog(self._conn, item_id, parent=self).exec():
            # Data changed — close so the inventory refreshes with new values.
            self.accept()

    def _header(self, item) -> QLabel:
        if item is None:
            return QLabel("Item not found.")
        bits = [f"<b>{item.name}</b>"]
        if item.set_name:
            bits.append(item.set_name)
        if item.card_number:
            bits.append(f"#{item.card_number}")
        bits.append(item.language.value)
        if item.is_graded:
            company = item.grading_company.value if item.grading_company else "?"
            bits.append(f"{company} {item.grade or ''}".strip())
        elif item.condition:
            bits.append(item.condition.value)
        label = QLabel(" · ".join(bits))
        label.setTextFormat(Qt.RichText)
        return label

    def _position_line(self, item_id: int) -> QLabel:
        pos = InventoryService(self._conn).item_position(item_id)
        return QLabel(
            f"On hand: {pos.remaining_qty}   ·   "
            f"Cost basis: {money(pos.cost_basis_cents)}   ·   "
            f"Market: {money(pos.market_value_cents)}   ·   "
            f"Unrealized P/L: {money(pos.unrealized_pl_cents)}"
        )

    def _lots_table(self, item_id: int) -> QTableWidget:
        lots = LotRepository(self._conn).list_for_item(item_id)
        headers = [
            "SKU",
            "Acquired",
            "Qty",
            "Remaining",
            "Unit Cost",
            "Mkt @ Acq",
            "Intent",
            "Source",
        ]
        table = QTableWidget(len(lots), len(headers))
        table.setHorizontalHeaderLabels(headers)
        for r, lot in enumerate(lots):
            table.setItem(r, 0, _readonly_item(lot.sku or ""))
            table.setItem(r, 1, _readonly_item(lot.acquired_at))
            table.setItem(r, 2, _readonly_item(str(lot.quantity), numeric=True))
            table.setItem(r, 3, _readonly_item(str(lot.remaining_qty), numeric=True))
            table.setItem(
                r, 4, _readonly_item(money(lot.unit_cost_cents), numeric=True)
            )
            table.setItem(
                r,
                5,
                _readonly_item(money(lot.acquired_market_value_cents), numeric=True),
            )
            table.setItem(r, 6, _readonly_item(lot.intent.value))
            table.setItem(r, 7, _readonly_item(lot.source or ""))
        _tidy(table)
        return table

    def _history_table(self, item_id: int) -> QTableWidget:
        lines = TransactionLineRepository(self._conn).list_for_item(item_id)
        txn_repo = TransactionRepository(self._conn)
        headers = ["Date", "Type", "Dir", "Qty", "Unit Price", "Realized", "Status"]
        table = QTableWidget(len(lines), len(headers))
        table.setHorizontalHeaderLabels(headers)
        for r, line in enumerate(lines):
            txn = txn_repo.get(line.transaction_id)
            table.setItem(r, 0, _readonly_item(txn.occurred_at if txn else ""))
            table.setItem(r, 1, _readonly_item(txn.type.value if txn else ""))
            table.setItem(r, 2, _readonly_item(line.direction.value))
            table.setItem(r, 3, _readonly_item(str(line.quantity), numeric=True))
            table.setItem(
                r, 4, _readonly_item(money(line.unit_price_cents), numeric=True)
            )
            table.setItem(
                r, 5, _readonly_item(money(line.realized_pl_cents), numeric=True)
            )
            table.setItem(r, 6, _readonly_item(txn.status.value if txn else ""))
        _tidy(table)
        return table


def _tidy(table: QTableWidget) -> None:
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    table.resizeColumnsToContents()
