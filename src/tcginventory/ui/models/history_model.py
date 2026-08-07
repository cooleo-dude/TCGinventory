"""Qt table model over transactions for the History view (read-only)."""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor, QFont

from tcginventory.domain.enums import TransactionStatus
from tcginventory.domain.models import Transaction
from tcginventory.ui import theme
from tcginventory.ui.formatting import money


def _pos() -> QColor:
    return QColor(theme.active().pos)


def _neg() -> QColor:
    return QColor(theme.active().neg)


def _muted() -> QColor:
    return QColor(theme.active().ink3)


_ROOT = QModelIndex()  # module-level singleton for default parent args

_HEADERS = [
    "Date",
    "Type",
    "Counterparty",
    "Cash",
    "Margin",
    "Status",
    "Cards",
    "Items",
]

# Column of the card-art thumbnail stack, and of the clickable item-list field.
CARDS_COL = _HEADERS.index("Cards")
ITEMS_COL = _HEADERS.index("Items")

# Role the thumbnail delegate reads: (thumb_card_ids, total_card_count).
CARDS_ROLE = Qt.UserRole + 1

# Plain ints for the roles ``data()`` tests — see inventory_model for why. The
# grid's sort accounts for 100% of the calls during a reset (5,132 for 262 rows,
# 94.9% of them repeats), and comparing wrapped C++ enums is what made it slow.
_R_DISPLAY = int(Qt.DisplayRole)
_R_TOOLTIP = int(Qt.ToolTipRole)
_R_FONT = int(Qt.FontRole)
_R_ALIGN = int(Qt.TextAlignmentRole)
_R_FOREGROUND = int(Qt.ForegroundRole)
_R_SORT = int(Qt.UserRole)
_R_CARDS = int(CARDS_ROLE)
_ALIGN_RIGHT = int(Qt.AlignRight | Qt.AlignVCenter)

_MISS = object()  # distinguishes "not cached" from a cached None


@dataclass(frozen=True)
class HistoryRow:
    transaction: Transaction
    margin_cents: int
    proceeds_cents: int  # 0 for pure purchases → no margin shown
    items_label: str = ""  # "Articuno 161/159, Charizard 004/102" (elided in grid)
    thumb_ids: list[str | None] = field(default_factory=list)  # first ≤3 card ids
    card_count: int = 0  # total distinct cards in the transaction (for "+N")


class HistoryTableModel(QAbstractTableModel):
    def __init__(self, rows: list[HistoryRow] | None = None) -> None:
        super().__init__()
        self._rows: list[HistoryRow] = rows or []
        # (row, column) -> sort key, derived from _rows and dropped with them.
        self._sort_cache: dict[tuple[int, int], object] = {}

    def set_rows(self, rows: list[HistoryRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self._sort_cache.clear()  # keys are derived from the rows being replaced
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
        role = int(role)
        col = index.column()

        # The sort is the whole cost of a reset here, and it asks for the same
        # cell up to 124 times. Answer it first, and answer repeats from cache.
        if role == _R_SORT:
            key = (index.row(), col)
            cached = self._sort_cache.get(key, _MISS)
            if cached is _MISS:  # a sentinel, so a cached None is still a hit
                row = self._rows[index.row()]
                cached = self._sort_key(row, row.transaction, col)
                self._sort_cache[key] = cached
            return cached

        row = self._rows[index.row()]
        txn = row.transaction
        voided = txn.status is TransactionStatus.VOIDED

        if role == _R_DISPLAY:
            return self._display(row, txn, col)
        if role == _R_CARDS and col == CARDS_COL:
            return (row.thumb_ids, row.card_count)
        if role == _R_TOOLTIP and col == CARDS_COL and row.items_label:
            return row.items_label
        if role == _R_TOOLTIP and col == ITEMS_COL and row.items_label:
            return row.items_label + "\n\n(click to view the full list)"
        if role == _R_ALIGN and col in (3, 4):
            return _ALIGN_RIGHT
        if role == _R_FONT and col in (0, 3, 4):  # date + money → mono, aligned
            return QFont(theme.mono_family())
        if role == _R_FOREGROUND:
            if voided:
                return QBrush(_muted())
            if col == 3 and txn.cash_delta_cents != 0:
                return QBrush(_pos() if txn.cash_delta_cents > 0 else _neg())
            if col == 4 and row.proceeds_cents and row.margin_cents != 0:
                return QBrush(_pos() if row.margin_cents > 0 else _neg())
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
        return ""  # CARDS_COL is painted by a delegate, no display text

    @staticmethod
    def _sort_key(row: HistoryRow, txn: Transaction, col: int) -> object:
        return {
            0: txn.occurred_at,
            1: txn.type.value,
            2: (txn.counterparty or "").lower(),
            3: txn.cash_delta_cents,
            4: row.margin_cents,
            5: txn.status.value,
            CARDS_COL: row.card_count,
            ITEMS_COL: row.items_label.lower(),
        }[col]
