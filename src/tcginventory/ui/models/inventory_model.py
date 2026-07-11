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
        "Avg cost",
        lambda r: money(r.avg_cost_cents),
        lambda r: r.avg_cost_cents,
        numeric=True,
    ),
    _Column(
        "Cost basis",
        lambda r: money(r.cost_basis_cents),
        lambda r: r.cost_basis_cents,
        numeric=True,
    ),
    _Column(
        "Market at ToP",
        lambda r: money(r.market_value_cents),
        lambda r: r.market_value_cents,
        numeric=True,
    ),
    _Column(
        "Unrealized P/L",
        lambda r: money(r.unrealized_pl_cents),
        lambda r: r.unrealized_pl_cents,
        numeric=True,
        pl=True,
    ),
    _Column("Sold", lambda r: r.sold_status, lambda r: r.sold_status),
    _Column(
        "Location",
        lambda r: r.item.location or "",
        lambda r: (r.item.location or "").lower(),
    ),
]


class InventoryTableModel(QAbstractTableModel):
    def __init__(self, rows: list[InventoryRow] | None = None) -> None:
        super().__init__()
        self._rows: list[InventoryRow] = rows or []

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
        return None
