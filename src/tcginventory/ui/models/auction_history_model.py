"""Qt table model over ``auction_event`` for the Auction History view.

Mirrors the transaction ``HistoryTableModel`` and the recognition log
one-for-one: one row per thing that happened to a watched auction — deleted,
archived, unarchived, or won (its cards posted into the ledger).

The pill columns sit at the **same slots** the other two logs use (1 and 5), so
``_PillDelegate`` paints the right cells without a per-model column map.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QFont

from tcginventory.data.auctions import EVENT_WON, AuctionEvent
from tcginventory.ui import theme
from tcginventory.ui.formatting import money

_ROOT = QModelIndex()

_HEADERS = [
    "Date",
    "Event",
    "Auction",
    "Cards",
    "Value",
    "State",
    "Result",
]

# Same slots the transaction log uses: pill = 1, pill = 5 (see _PillDelegate).
EVENT_COL = _HEADERS.index("Event")
TITLE_COL = _HEADERS.index("Auction")
CARDS_COL = _HEADERS.index("Cards")
VALUE_COL = _HEADERS.index("Value")
STATE_COL = _HEADERS.index("State")
RESULT_COL = _HEADERS.index("Result")

_UNTITLED = "(untitled)"


@dataclass(frozen=True)
class AuctionEventRow:
    """One ``auction_event``, flattened for display."""

    event: AuctionEvent

    @property
    def state(self) -> str:
        """What can still be done about this row.

        ``restorable`` is the only actionable state; a delete that has been
        undone reads ``restored``, and everything else is simply ``logged``.
        """
        if self.event.restorable:
            return "restorable"
        if self.event.restored_at is not None:
            return "restored"
        return "logged"

    @property
    def result(self) -> str:
        """The consequence, in one phrase — what the row is *for*."""
        e = self.event
        if e.restored_id is not None:
            return f"restored as #{e.restored_id}"
        if e.kind == EVENT_WON and e.transaction_id is not None:
            # A loose pointer into inventory.db, shown but never resolved: the
            # ledger may have voided it since, or been restored from a backup.
            return f"transaction {e.transaction_id}"
        return ""


class AuctionHistoryModel(QAbstractTableModel):
    def __init__(self, rows: list[AuctionEventRow] | None = None) -> None:
        super().__init__()
        self._rows: list[AuctionEventRow] = rows or []

    def set_rows(self, rows: list[AuctionEventRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, source_row: int) -> AuctionEventRow:
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
        col = index.column()
        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == Qt.UserRole:
            return self._sort_key(row, col)
        if role == Qt.ToolTipRole and col == TITLE_COL:
            return row.event.title or _UNTITLED
        if role == Qt.TextAlignmentRole and col in (CARDS_COL, VALUE_COL):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.FontRole and col in (0, CARDS_COL, VALUE_COL):
            return QFont(theme.mono_family())
        return None

    @staticmethod
    def _display(row: AuctionEventRow, col: int) -> str:
        e = row.event
        if col == 0:
            # Stored UTC ISO; shown to the minute. Sorting uses the raw string
            # (_sort_key), so trimming here cannot reorder the log.
            return e.occurred_at[:16].replace("T", " ")
        if col == EVENT_COL:
            return e.kind
        if col == TITLE_COL:
            return e.title or _UNTITLED
        if col == CARDS_COL:
            return str(e.card_count)
        if col == VALUE_COL:
            return money(e.value_cents)
        if col == STATE_COL:
            return row.state
        if col == RESULT_COL:
            return row.result
        return ""

    @staticmethod
    def _sort_key(row: AuctionEventRow, col: int) -> object:
        e = row.event
        return {
            0: e.occurred_at,
            EVENT_COL: e.kind,
            TITLE_COL: (e.title or _UNTITLED).lower(),
            CARDS_COL: e.card_count,
            VALUE_COL: e.value_cents,
            STATE_COL: row.state,
            RESULT_COL: row.result.lower(),
        }[col]
