"""Qt table model over vision captures for the Recognition History view.

Mirrors the transaction ``HistoryTableModel`` format one-for-one, but each row is
a **capture** (one annotation session) and the figures describe the training
corpus: images supplied, boxes drawn, and how many carry an identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QFont

from tcginventory.ui import theme

_ROOT = QModelIndex()

_HEADERS = [
    "Date",
    "Source",
    "Export",
    "Images",
    "Labeled",
    "State",
    "Cards",
    "Items",
]

# Same slots the transaction log uses, so the shared pill/thumbnail delegates
# paint the right columns: Source pill = 1, State pill = 5, thumbnails = 6.
SOURCE_COL = _HEADERS.index("Source")
EXPORT_COL = _HEADERS.index("Export")
STATE_COL = _HEADERS.index("State")
CARDS_COL = _HEADERS.index("Cards")
ITEMS_COL = _HEADERS.index("Items")
CARDS_ROLE = Qt.UserRole + 1  # (thumb_card_ids, total_card_count)
EXPORT_ROLE = Qt.UserRole + 2  # (capture_id, exportable_card_count)


@dataclass(frozen=True)
class RecognitionRow:
    capture_id: int
    source: str
    note: str
    occurred_at: str
    images: int
    regions: int
    labeled: int
    items_label: str = ""
    thumb_ids: list[str | None] = field(default_factory=list)
    card_count: int = 0

    @property
    def state(self) -> str:
        """Labeling progress of the session's boxes."""
        if self.regions == 0:
            return "unlabeled"
        if self.labeled >= self.regions:
            return "labeled"
        if self.labeled == 0:
            return "unlabeled"
        return "partial"


class RecognitionHistoryModel(QAbstractTableModel):
    def __init__(self, rows: list[RecognitionRow] | None = None) -> None:
        super().__init__()
        self._rows: list[RecognitionRow] = rows or []

    def set_rows(self, rows: list[RecognitionRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, source_row: int) -> RecognitionRow:
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
        if role == CARDS_ROLE and col == CARDS_COL:
            return (row.thumb_ids, row.card_count)
        if role == EXPORT_ROLE and col == EXPORT_COL:
            return (row.capture_id, row.card_count)
        if role == Qt.ToolTipRole and col in (CARDS_COL, ITEMS_COL) and row.items_label:
            return row.items_label
        # The note lost its own column to the export buttons — keep it readable.
        if role == Qt.ToolTipRole and col in (0, SOURCE_COL) and row.note:
            return row.note
        if role == Qt.TextAlignmentRole and col in (3, 4):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.FontRole and col in (0, 3, 4):  # date + counts → mono
            return QFont(theme.mono_family())
        return None

    @staticmethod
    def _display(row: RecognitionRow, col: int) -> str:
        if col == 0:
            return row.occurred_at
        if col == SOURCE_COL:
            return row.source or "manual"
        if col == EXPORT_COL:
            return ""  # painted by the export-buttons delegate
        if col == 3:
            return str(row.images)
        if col == 4:
            return f"{row.labeled}/{row.regions}"
        if col == STATE_COL:
            return row.state
        if col == ITEMS_COL:
            return row.items_label
        return ""  # CARDS_COL is painted by a delegate

    @staticmethod
    def _sort_key(row: RecognitionRow, col: int) -> object:
        return {
            0: row.occurred_at,
            SOURCE_COL: row.source or "",
            EXPORT_COL: row.card_count,  # sorts exportable captures together
            3: row.images,
            4: row.labeled,
            STATE_COL: row.state,
            CARDS_COL: row.card_count,
            ITEMS_COL: row.items_label.lower(),
        }[col]
