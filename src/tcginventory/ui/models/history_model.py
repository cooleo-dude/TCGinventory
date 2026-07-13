"""Qt table model over transactions for the History view (read-only)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from tcginventory.domain.enums import TransactionStatus
from tcginventory.domain.models import Transaction
from tcginventory.ui.formatting import money

_GREEN = QColor("#2e7d32")
_RED = QColor("#c62828")
_MUTED = QColor("#9e9e9e")
_ROOT = QModelIndex()  # module-level singleton for default parent args

_HEADERS = ["Date", "Type", "Counterparty", "Cash", "Margin", "Status", "Items"]

# Column index of the clickable item-list field (the view opens the full list).
ITEMS_COL = _HEADERS.index("Items")


@dataclass(frozen=True)
class HistoryRow:
    transaction: Transaction
    margin_cents: int
    proceeds_cents: int  # 0 for pure purchases → no margin shown
    items_label: str = ""  # "Articuno 161/159, Charizard 004/102" (elided in grid)


class HistoryTableModel(QAbstractTableModel):
    def __init__(self, rows: list[HistoryRow] | None = None) -> None:
        super().__init__()
        self._rows: list[HistoryRow] = rows or []

    def set_rows(self, rows: list[HistoryRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, source_row: int) -> HistoryRow:
        return self._rows[source_row]

    # -- Qt API -------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = _ROOT) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = _ROOT) -> int:
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        txn = row.transaction
        col = index.column()
        voided = txn.status is TransactionStatus.VOIDED

        if role == Qt.DisplayRole:
            return self._display(row, txn, col)
        if role == Qt.UserRole:
            return self._sort_key(row, txn, col)
        if role == Qt.ToolTipRole and col == ITEMS_COL and row.items_label:
            return row.items_label + "\n\n(click to view the full list)"
        if role == Qt.TextAlignmentRole and col in (3, 4):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if voided:
                return QBrush(_MUTED)
            if col == 3 and txn.cash_delta_cents != 0:
                return QBrush(_GREEN if txn.cash_delta_cents > 0 else _RED)
            if col == 4 and row.proceeds_cents and row.margin_cents != 0:
                return QBrush(_GREEN if row.margin_cents > 0 else _RED)
        return None

    @staticmethod
    def _display(row: HistoryRow, txn: Transaction, col: int) -> str:
        if col == 0:
            return txn.occurred_at
        if col == 1:
            return txn.type.value
        if col == 2:
            return txn.counterparty or ""
        if col == 3:
            return money(txn.cash_delta_cents)
        if col == 4:
            return money(row.margin_cents) if row.proceeds_cents else ""
        if col == 5:
            return txn.status.value
        if col == ITEMS_COL:
            return row.items_label
        return ""

    @staticmethod
    def _sort_key(row: HistoryRow, txn: Transaction, col: int) -> object:
        return {
            0: txn.occurred_at,
            1: txn.type.value,
            2: (txn.counterparty or "").lower(),
            3: txn.cash_delta_cents,
            4: row.margin_cents,
            5: txn.status.value,
            ITEMS_COL: row.items_label.lower(),
        }[col]
