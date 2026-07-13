"""History view — full transaction log with detail drill-down (F-VW-3).

Read-only in M3 (Void/Edit actions arrive in M4).
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import (
    ItemRepository,
    LotConsumptionRepository,
    LotRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import Direction, TransactionStatus
from tcginventory.domain.models import Item
from tcginventory.services.exceptions import ServiceError
from tcginventory.services.reporting import ReportingService
from tcginventory.services.transactions import TransactionService
from tcginventory.ui.formatting import money, percent
from tcginventory.ui.models.history_model import HistoryRow, HistoryTableModel

_TYPES = ["All", "purchase", "sale", "trade", "adjustment"]

# Bottom-pane columns describing each card in the selected transaction.
_DETAIL_HEADERS = [
    "SKU",
    "Name",
    "Set",
    "#",
    "Purchase Price",
    "Notes",
    "Cond/Grade",
    "Language",
]


def _cond_grade(item: Item) -> str:
    if item.is_graded:
        company = item.grading_company.value if item.grading_company else "?"
        return f"{company} {item.grade or ''}".strip()
    return item.condition.value if item.condition else ""


class _HistoryFilterProxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.setSortRole(Qt.UserRole)
        self._type = "All"
        self._text = ""

    def set_type(self, value: str) -> None:
        self._type = value
        self.invalidateFilter()

    def set_text(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        txn = self.sourceModel().row_at(source_row).transaction
        if self._type != "All" and txn.type.value != self._type:
            return False
        if self._text and self._text not in (txn.counterparty or "").lower():
            return False
        return True


class HistoryView(QWidget):
    edit_requested = Signal(int)  # transaction id to edit
    changed = Signal()  # a void changed the ledger

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._txns = TransactionRepository(conn)
        self._lines = TransactionLineRepository(conn)
        self._consumptions = LotConsumptionRepository(conn)
        self._lots = LotRepository(conn)
        self._items = ItemRepository(conn)
        self._reporting = ReportingService(conn)
        self._service = TransactionService(conn)

        self._model = HistoryTableModel()
        self._proxy = _HistoryFilterProxy()
        self._proxy.setSourceModel(self._model)

        self._type = QComboBox()
        self._type.addItems(_TYPES)
        self._type.currentTextChanged.connect(self._proxy.set_type)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search counterparty…")
        self._search.textChanged.connect(self._proxy.set_text)

        self._edit_btn = QPushButton("Edit…")
        self._edit_btn.clicked.connect(self._edit_selected)
        self._void_btn = QPushButton("Void…")
        self._void_btn.clicked.connect(self._void_selected)

        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Type:"))
        filter_bar.addWidget(self._type)
        filter_bar.addWidget(self._search, 1)
        filter_bar.addWidget(self._edit_btn)
        filter_bar.addWidget(self._void_btn)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.AscendingOrder)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.selectionModel().selectionChanged.connect(self._show_detail)

        # Bottom pane: transaction summary + a table of its cards.
        self._detail_header = QLabel("Select a transaction to see its items.")
        self._detail_header.setStyleSheet("padding: 4px;")
        self._detail_table = QTableWidget(0, len(_DETAIL_HEADERS))
        self._detail_table.setHorizontalHeaderLabels(_DETAIL_HEADERS)
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._detail_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._detail_table.setAlternatingRowColors(True)
        self._detail_table.horizontalHeader().setStretchLastSection(True)

        detail_box = QWidget()
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self._detail_header)
        detail_layout.addWidget(self._detail_table)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._table)
        splitter.addWidget(detail_box)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(filter_bar)
        layout.addWidget(splitter)

        self.refresh()

    def refresh(self) -> None:
        rows = [
            HistoryRow(
                transaction=txn,
                margin_cents=self._reporting.transaction_margin_cents(txn.id),
                proceeds_cents=self._reporting.transaction_proceeds_cents(txn.id),
                items_label=", ".join(self._item_labels(txn.id)),
            )
            for txn in self._txns.list_all()
        ]
        self._model.set_rows(rows)
        self._table.resizeColumnsToContents()
        self._clear_detail()

    def _item_labels(self, transaction_id: int) -> list[str]:
        """['Articuno 161/159', 'Charizard 004/102', …] for a transaction."""
        labels = []
        for line in self._lines.list_for_transaction(transaction_id):
            item = self._items.get(line.item_id)
            name = item.name if item else f"item {line.item_id}"
            number = (item.card_number if item else None) or ""
            labels.append(f"{name} {number}".strip())
        return labels

    def _selected_transaction(self):
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        source = self._proxy.mapToSource(indexes[0])
        return self._model.row_at(source.row()).transaction

    def _edit_selected(self) -> None:
        txn = self._selected_transaction()
        if txn is None:
            QMessageBox.information(self, "Edit", "Select a transaction first.")
            return
        if txn.status is TransactionStatus.VOIDED:
            QMessageBox.information(self, "Edit", "That transaction is voided.")
            return
        self.edit_requested.emit(txn.id)

    def _void_selected(self) -> None:
        txn = self._selected_transaction()
        if txn is None:
            QMessageBox.information(self, "Void", "Select a transaction first.")
            return
        if txn.status is TransactionStatus.VOIDED:
            QMessageBox.information(self, "Void", "That transaction is already voided.")
            return
        confirm = QMessageBox.question(
            self,
            "Void transaction",
            f"Void transaction {txn.id} ({txn.type.value})? This reverses its "
            "effects but keeps it in history.",
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._service.void(txn.id)
        except ServiceError as exc:
            QMessageBox.warning(self, "Could not void", str(exc))
            return
        self.refresh()
        self.changed.emit()

    def _clear_detail(self) -> None:
        self._detail_header.setText("Select a transaction to see its items.")
        self._detail_table.setRowCount(0)

    def _show_detail(self) -> None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            self._clear_detail()
            return
        source = self._proxy.mapToSource(indexes[0])
        row = self._model.row_at(source.row())
        txn = row.transaction

        summary = (
            f"{txn.type.value.title()} — {txn.occurred_at}   ·   "
            f"Status: {txn.status.value}   ·   Cash: {money(txn.cash_delta_cents)}"
        )
        if txn.counterparty:
            summary += f"   ·   {txn.counterparty}"
        if row.proceeds_cents:
            summary += (
                f"   ·   Margin: {money(row.margin_cents)} "
                f"({percent(self._reporting.gross_margin_pct(txn.id))})"
            )
        self._detail_header.setText(summary)

        cells = self._detail_rows(txn.id)
        self._detail_table.setRowCount(len(cells))
        for r, values in enumerate(cells):
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c == 4:  # Purchase Price → right-aligned
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._detail_table.setItem(r, c, item)
        self._detail_table.resizeColumnsToContents()
        self._detail_table.horizontalHeader().setStretchLastSection(True)

    def _detail_rows(self, transaction_id: int) -> list[list[str]]:
        """One row per lot in the transaction, with the requested columns."""
        rows: list[list[str]] = []
        for line in self._lines.list_for_transaction(transaction_id):
            item = self._items.get(line.item_id)
            if item is None:
                continue
            if line.direction is Direction.IN:
                lots = self._lots.list_for_acquired_line(line.id)
                pairs = [(lot.sku or "", lot.unit_cost_cents) for lot in lots]
            else:  # OUT — draw price basis from the lots it consumed
                pairs = []
                for cons in self._consumptions.list_for_line(line.id):
                    lot = self._lots.get(cons.lot_id)
                    sku = (lot.sku if lot else "") or ""
                    pairs.append((sku, cons.unit_cost_cents))
            if not pairs:  # fallback so the card still shows
                pairs = [("", line.unit_price_cents)]
            for sku, price_cents in pairs:
                rows.append(
                    [
                        sku,
                        item.name,
                        item.set_name or "",
                        item.card_number or "",
                        money(price_cents),
                        item.notes or "",
                        _cond_grade(item),
                        item.language.value,
                    ]
                )
        return rows
