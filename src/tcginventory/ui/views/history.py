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
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.repositories import (
    ItemRepository,
    LotConsumptionRepository,
    TransactionLineRepository,
    TransactionRepository,
)
from tcginventory.domain.enums import Direction, TransactionStatus
from tcginventory.services.exceptions import ServiceError
from tcginventory.services.reporting import ReportingService
from tcginventory.services.transactions import TransactionService
from tcginventory.ui.formatting import money, percent
from tcginventory.ui.models.history_model import HistoryRow, HistoryTableModel

_TYPES = ["All", "purchase", "sale", "trade", "adjustment"]


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

        self._detail = QTextBrowser()
        self._detail.setPlaceholderText("Select a transaction to see its detail.")

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._table)
        splitter.addWidget(self._detail)
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
            )
            for txn in self._txns.list_all()
        ]
        self._model.set_rows(rows)
        self._table.resizeColumnsToContents()
        self._detail.clear()

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

    def _show_detail(self) -> None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            self._detail.clear()
            return
        source = self._proxy.mapToSource(indexes[0])
        row = self._model.row_at(source.row())
        self._detail.setHtml(self._detail_html(row))

    def _detail_html(self, row: HistoryRow) -> str:
        txn = row.transaction
        parts = [
            f"<h3>{txn.type.value.title()} — {txn.occurred_at}</h3>",
            f"<p>Status: {txn.status.value}",
        ]
        if txn.counterparty:
            parts.append(f" &nbsp;·&nbsp; Counterparty: {txn.counterparty}")
        margin_text = money(row.margin_cents) if row.proceeds_cents else "—"
        parts.append(
            f"<br>Cash: {money(txn.cash_delta_cents)}"
            f" &nbsp;·&nbsp; Margin: {margin_text}"
        )
        if row.proceeds_cents:
            pct = self._reporting.gross_margin_pct(txn.id)
            parts.append(f" ({percent(pct)})")
        parts.append("</p>")

        parts.append(
            "<table width='100%' cellpadding='4'><tr>"
            "<th align='left'>Dir</th><th align='left'>Item</th>"
            "<th align='right'>Qty</th><th align='right'>Unit Price</th>"
            "<th align='right'>Realized</th></tr>"
        )
        for line in self._lines.list_for_transaction(txn.id):
            item = self._items.get(line.item_id)
            name = item.name if item else f"item {line.item_id}"
            realized = (
                money(line.realized_pl_cents)
                if line.realized_pl_cents is not None
                else ""
            )
            parts.append(
                f"<tr><td>{line.direction.value}</td><td>{name}</td>"
                f"<td align='right'>{line.quantity}</td>"
                f"<td align='right'>{money(line.unit_price_cents)}</td>"
                f"<td align='right'>{realized}</td></tr>"
            )
            if line.direction is Direction.OUT:
                for cons in self._consumptions.list_for_line(line.id):
                    parts.append(
                        f"<tr><td></td><td colspan='4' style='color:#777'>"
                        f"&nbsp;&nbsp;↳ drew {cons.quantity} from lot {cons.lot_id} "
                        f"@ {money(cons.unit_cost_cents)}</td></tr>"
                    )
        parts.append("</table>")
        return "".join(parts)
