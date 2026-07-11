"""Qt table model over :class:`InventoryRow` for the Inventory grid.

Read-only. Exposes a numeric ``Qt.UserRole`` sort key per cell so a
``QSortFilterProxyModel`` (``sortRole = Qt.UserRole``) sorts money/qty columns
numerically rather than lexically (F-VW-9).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from tcginventory.domain.models import Item
from tcginventory.services.inventory import InventoryRow
from tcginventory.ui.formatting import money

_GREEN = QColor("#2e7d32")
_RED = QColor("#c62828")
_ROOT = QModelIndex()  # module-level singleton for default parent args


def _cond_grade(item: Item) -> str:
    if item.is_graded:
        company = item.grading_company.value if item.grading_company else "?"
        return f"{company} {item.grade or ''}".strip()
    return item.condition.value if item.condition else ""


def _current_display(r: InventoryRow) -> str:
    return money(r.current_value_cents) if r.current_value_cents is not None else "—"


def _current_sort(r: InventoryRow) -> int:
    return r.current_value_cents if r.current_value_cents is not None else -1


def _upl(r: InventoryRow) -> int | None:
    if r.current_value_cents is None:
        return None
    return r.current_value_cents - r.cost_basis_cents


def _upl_display(r: InventoryRow) -> str:
    value = _upl(r)
    return money(value) if value is not None else "—"


def _upl_sort(r: InventoryRow) -> int:
    value = _upl(r)
    return value if value is not None else 0


@dataclass(frozen=True)
class _Column:
    header: str
    display: Callable[[InventoryRow], str]
    sort_key: Callable[[InventoryRow], object]
    numeric: bool = False
    pl: bool = False  # colour by sign


_COLUMNS: list[_Column] = [
    _Column("Name", lambda r: r.item.name, lambda r: r.item.name.lower()),
    _Column(
        "Set",
        lambda r: r.item.set_name or "",
        lambda r: (r.item.set_name or "").lower(),
    ),
    _Column(
        "#", lambda r: r.item.card_number or "", lambda r: r.item.card_number or ""
    ),
    _Column("Lang", lambda r: r.item.language.value, lambda r: r.item.language.value),
    _Column("Cond/Grade", lambda r: _cond_grade(r.item), lambda r: _cond_grade(r.item)),
    _Column("Intent", lambda r: r.intent, lambda r: r.intent),
    _Column("Qty", lambda r: str(r.on_hand), lambda r: r.on_hand, numeric=True),
    _Column(
        "Purchase Price",
        lambda r: money(r.cost_basis_cents),
        lambda r: r.cost_basis_cents,
        numeric=True,
    ),
    _Column(
        "Market at Purchase",
        lambda r: money(r.market_at_purchase_cents),
        lambda r: r.market_at_purchase_cents,
        numeric=True,
    ),
    # Live once prices are polled; "—" for items not yet priced.
    _Column("Market Price Current", _current_display, _current_sort, numeric=True),
    _Column("Unrealized P/L", _upl_display, _upl_sort, numeric=True, pl=True),
    _Column("Sold", lambda r: r.sold_status, lambda r: r.sold_status),
]


# Column index of "Market Price Current" (the review-editable column).
MARKET_CURRENT_COL = [c.header for c in _COLUMNS].index("Market Price Current")
# Extra roles the review delegate reads (sort role is UserRole; keep clear of it).
ITEM_ID_ROLE = Qt.UserRole + 1
REVIEW_ROLE = Qt.UserRole + 2
_AMBER = QColor("#fff3cd")


class InventoryTableModel(QAbstractTableModel):
    def __init__(
        self,
        rows: list[InventoryRow] | None = None,
        on_edit_price=None,  # (item_id, total_cents, on_hand) -> None
    ) -> None:
        super().__init__()
        self._rows: list[InventoryRow] = rows or []
        self._on_edit_price = on_edit_price

    def set_rows(self, rows: list[InventoryRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, source_row: int) -> InventoryRow:
        return self._rows[source_row]

    # -- Qt API -------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = _ROOT) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = _ROOT) -> int:
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return _COLUMNS[section].header
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = _COLUMNS[index.column()]
        if role == Qt.DisplayRole:
            return col.display(row)
        if role == Qt.UserRole:  # sort key
            return col.sort_key(row)
        if role == Qt.TextAlignmentRole and col.numeric:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole and col.pl:
            value = col.sort_key(row)
            if isinstance(value, int) and value != 0:
                return QBrush(_GREEN if value > 0 else _RED)
        if role == ITEM_ID_ROLE:
            return row.item.id
        if role == REVIEW_ROLE:
            return row.review
        if index.column() == MARKET_CURRENT_COL and role == Qt.EditRole:
            return (row.current_value_cents or 0) / 100.0
        if (
            role == Qt.BackgroundRole
            and index.column() == MARKET_CURRENT_COL
            and row.review
        ):
            return QBrush(_AMBER)
        return None

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if index.column() == MARKET_CURRENT_COL and self._rows[index.row()].review:
            base |= Qt.ItemIsEditable
        return base

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if index.column() != MARKET_CURRENT_COL or role != Qt.EditRole:
            return False
        row = self._rows[index.row()]
        try:
            total_cents = round(float(value) * 100)
        except (TypeError, ValueError):
            return False
        if self._on_edit_price is not None:
            self._on_edit_price(row.item.id, total_cents, row.on_hand)
        return True
