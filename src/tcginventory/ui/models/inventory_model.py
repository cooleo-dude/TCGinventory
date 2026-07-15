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

from tcginventory.domain.enums import Condition, Edition, HoloPattern, Language
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
    return money(r.current_price_cents) if r.current_price_cents is not None else "—"


def _current_sort(r: InventoryRow) -> int:
    return r.current_price_cents if r.current_price_cents is not None else -1


def _upl_display(r: InventoryRow) -> str:
    value = r.unrealized_cents
    return money(value) if value is not None else "—"


def _upl_sort(r: InventoryRow) -> int:
    value = r.unrealized_cents
    return value if value is not None else 0


@dataclass(frozen=True)
class _Column:
    header: str
    display: Callable[[InventoryRow], str]
    sort_key: Callable[[InventoryRow], object]
    numeric: bool = False
    pl: bool = False  # colour by sign
    # Inline editing: `field` is the Item attribute to edit; `editor` is the kind
    # ("text" or "combo"); `enum` is the enum class for combo columns.
    field: str | None = None
    editor: str | None = None
    enum: object = None


_COLUMNS: list[_Column] = [
    _Column("SKU", lambda r: r.sku, lambda r: r.sku),
    _Column(
        "Name", lambda r: r.item.name, lambda r: r.item.name.lower(),
        field="name", editor="text",
    ),
    _Column(
        "Set",
        lambda r: r.item.set_name or "",
        lambda r: (r.item.set_name or "").lower(),
        field="set_name", editor="text",
    ),
    _Column(
        "#", lambda r: r.item.card_number or "", lambda r: r.item.card_number or "",
        field="card_number", editor="text",
    ),
    _Column(
        "Lang", lambda r: r.item.language.value, lambda r: r.item.language.value,
        field="language", editor="combo", enum=Language,
    ),
    _Column(
        "Cond/Grade", lambda r: _cond_grade(r.item), lambda r: _cond_grade(r.item),
        field="condition", editor="combo", enum=Condition,
    ),
    _Column(
        "Holo",
        lambda r: r.item.holo_pattern.value if r.item.holo_pattern else "",
        lambda r: r.item.holo_pattern.value if r.item.holo_pattern else "",
        field="holo_pattern", editor="combo", enum=HoloPattern,
    ),
    _Column(
        "Edition",
        lambda r: r.item.edition.value if r.item.edition else "",
        lambda r: r.item.edition.value if r.item.edition else "",
        field="edition", editor="combo", enum=Edition,
    ),
    _Column("Intent", lambda r: r.intent, lambda r: r.intent),
    _Column("Acquired", lambda r: r.acquired_at, lambda r: r.acquired_at),
    _Column(
        "Purchase Price",
        lambda r: money(r.purchase_price_cents),
        lambda r: r.purchase_price_cents,
        numeric=True,
    ),
    _Column(
        "Market at Purchase",
        lambda r: money(r.market_at_purchase_cents),
        lambda r: r.market_at_purchase_cents,
        numeric=True,
    ),
    # Live once prices are polled; "—" for items not yet priced.
    _Column("Current Market", _current_display, _current_sort, numeric=True),
    _Column("Unrealized Gain/Loss", _upl_display, _upl_sort, numeric=True, pl=True),
    _Column("Sold", lambda r: r.sold_status, lambda r: r.sold_status),
    _Column(
        "Notes",
        lambda r: r.item.notes or "",
        lambda r: (r.item.notes or "").lower(),
        field="notes", editor="text",
    ),
]


def combo_columns() -> list[tuple[int, list[str]]]:
    """(column index, option-value list) for each combo-editable column, so the
    view can attach a drop-down delegate."""
    out: list[tuple[int, list[str]]] = []
    for i, col in enumerate(_COLUMNS):
        if col.editor == "combo" and col.enum is not None:
            out.append((i, [e.value for e in col.enum]))
    return out


# Column index of "Current Market" (the review-editable column).
MARKET_CURRENT_COL = [c.header for c in _COLUMNS].index("Current Market")
# Column index of "Intent" (edited inline via a combo-box delegate).
INTENT_COL = [c.header for c in _COLUMNS].index("Intent")
# Column index of "Notes" (long text → shown in full via a tooltip).
NOTES_COL = [c.header for c in _COLUMNS].index("Notes")
# Extra roles the review delegate reads (sort role is UserRole; keep clear of it).
ITEM_ID_ROLE = Qt.UserRole + 1
REVIEW_ROLE = Qt.UserRole + 2
LOT_ID_ROLE = Qt.UserRole + 3
# Review-cell highlight — gruvbox "neutral yellow" (#d79921), muted vs the old
# near-white amber. Shared with ReviewPriceDelegate so both stay in sync.
REVIEW_BG = QColor("#d79921")


class InventoryTableModel(QAbstractTableModel):
    def __init__(
        self,
        rows: list[InventoryRow] | None = None,
        on_edit_price=None,  # (item_id, per_unit_cents) -> None
        on_edit_intent=None,  # (lot_id, intent_str) -> None
        on_edit_item=None,  # (item_id, field, value) -> None : inline item edit
    ) -> None:
        super().__init__()
        self._rows: list[InventoryRow] = rows or []
        self._on_edit_price = on_edit_price
        self._on_edit_intent = on_edit_intent
        self._on_edit_item = on_edit_item

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
        if role == Qt.ToolTipRole:
            if index.column() == NOTES_COL and row.item.notes:
                return row.item.notes
            if index.column() == MARKET_CURRENT_COL and row.review:
                return "Reviewed price. Click ✕ to unmark, then ↻ to refresh."
        if role == ITEM_ID_ROLE:
            return row.item.id
        if role == REVIEW_ROLE:
            return row.review
        if role == LOT_ID_ROLE:
            return row.lot_id
        if index.column() == MARKET_CURRENT_COL and role == Qt.EditRole:
            return (row.current_price_cents or 0) / 100.0
        if index.column() == INTENT_COL and role == Qt.EditRole:
            return row.intent
        if role == Qt.EditRole:
            col = _COLUMNS[index.column()]
            if col.field is not None:  # inline-editable item field
                val = getattr(row.item, col.field)
                if col.enum is not None:
                    return val.value if val is not None else ""
                return val or ""
        if (
            role == Qt.BackgroundRole
            and index.column() == MARKET_CURRENT_COL
            and row.review
        ):
            return QBrush(REVIEW_BG)
        return None

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        col = index.column()
        row = self._rows[index.row()]
        if col == MARKET_CURRENT_COL:  # always editable → sets a manual price
            base |= Qt.ItemIsEditable
        elif col == INTENT_COL:
            base |= Qt.ItemIsEditable
        else:
            coldef = _COLUMNS[col]
            if coldef.field is not None:
                # Condition only applies to ungraded cards.
                if not (coldef.field == "condition" and row.item.is_graded):
                    base |= Qt.ItemIsEditable
        return base

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if role != Qt.EditRole:
            return False
        row = self._rows[index.row()]
        if index.column() == INTENT_COL:
            if str(value) == row.intent:
                return True  # no-op: don't trigger a refresh
            if self._on_edit_intent is not None:
                self._on_edit_intent(row.lot_id, str(value))
            return True
        if index.column() == MARKET_CURRENT_COL:
            try:
                per_unit_cents = round(float(value) * 100)
            except (TypeError, ValueError):
                return False
            if self._on_edit_price is not None:
                self._on_edit_price(row.item.id, per_unit_cents)
            return True
        coldef = _COLUMNS[index.column()]
        if coldef.field is not None:
            current = self.data(index, Qt.EditRole)
            if str(value).strip() == str(current).strip():
                return True  # no-op: closing an editor unchanged shouldn't refresh
            if self._on_edit_item is not None:
                self._on_edit_item(row.item.id, coldef.field, value)
            return True
        return False
