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
from tcginventory.services.ebay import sold_search_url
from tcginventory.services.inventory import InventoryRow
from tcginventory.services.trends import Trend
from tcginventory.ui.formatting import money

_GREEN = QColor("#2e7d32")
_RED = QColor("#c62828")
_GREY = QColor("#928374")  # flat / unknown trend
_ROOT = QModelIndex()  # module-level singleton for default parent args

_ARROW_UP = "▲"  # ▲
_ARROW_DOWN = "▼"  # ▼


def _trend_text(trend: Trend | None) -> str:
    """Direction as arrows; count grows with the move's magnitude."""
    if trend is None or trend.direction == "none":
        return "·"  # no data yet
    if trend.direction == "flat":
        return "–"
    glyph = _ARROW_UP if trend.direction == "up" else _ARROW_DOWN
    return glyph * max(1, trend.magnitude)


def _trend_color(trend: Trend | None) -> QColor:
    if trend is None or trend.direction in ("none", "flat"):
        return _GREY
    return _GREEN if trend.direction == "up" else _RED


def _trend_sort(trend: Trend | None) -> float:
    """Signed % change so the column sorts biggest gainers → biggest losers."""
    if trend is None or trend.pct_over_window is None:
        return 0.0
    return trend.pct_over_window


def _trend_tooltip(trend: Trend | None) -> str:
    if trend is None or trend.direction == "none":
        return "Not enough price history yet to detect a trend."
    pct = trend.pct_over_window or 0.0
    per_day = trend.slope_cents_per_day / 100.0
    return (
        f"{pct:+.1f}% over {trend.window_days}d "
        f"({per_day:+.2f}/day · {trend.points} pts)"
    )


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


def _sold_display(r: InventoryRow) -> str:
    return money(r.sold_price_cents) if r.sold_price_cents is not None else "—"


def _sold_sort(r: InventoryRow) -> int:
    return r.sold_price_cents if r.sold_price_cents is not None else -1


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
    # Link columns render as an open-URL button (shown only in the links panel):
    # "tcgplayer" | "ebay".
    link: str | None = None


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
    # Trend arrows are painted from the model's per-item trend map (set_trends),
    # so these lambdas are placeholders — data() intercepts the Trend column.
    _Column("Trend", lambda r: "", lambda r: 0.0),
    _Column("Unrealized G/L", _upl_display, _upl_sort, numeric=True, pl=True),
    _Column("Sold", lambda r: r.sold_status, lambda r: r.sold_status),
    _Column("Sold For", _sold_display, _sold_sort, numeric=True),
    _Column(
        "Notes",
        lambda r: r.item.notes or "",
        lambda r: (r.item.notes or "").lower(),
        field="notes", editor="text",
    ),
    # Link buttons — surfaced in the Inventory links panel, hidden in the grid.
    _Column(
        "TCGplayer", lambda r: "", lambda r: 1 if r.item.tcgplayer_url else 0,
        link="tcgplayer",
    ),
    _Column(
        "eBay", lambda r: "", lambda r: 1 if r.item.ebay_url else 0, link="ebay",
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
# Column index of "Market at Purchase" (editable acquisition snapshot; a record
# correction only — never feeds cost basis or P/L).
MARKET_AT_PURCHASE_COL = [c.header for c in _COLUMNS].index("Market at Purchase")
# Column index of "Trend" (arrows painted from the model's trend map).
TREND_COL = [c.header for c in _COLUMNS].index("Trend")
# Column index of "Intent" (edited inline via a combo-box delegate).
INTENT_COL = [c.header for c in _COLUMNS].index("Intent")
# Column index of "Notes" (long text → shown in full via a tooltip).
NOTES_COL = [c.header for c in _COLUMNS].index("Notes")
# Link-button columns (rendered in the Inventory links panel).
TCGPLAYER_COL = [c.header for c in _COLUMNS].index("TCGplayer")
EBAY_COL = [c.header for c in _COLUMNS].index("eBay")
# Extra roles the review delegate reads (sort role is UserRole; keep clear of it).
ITEM_ID_ROLE = Qt.UserRole + 1
REVIEW_ROLE = Qt.UserRole + 2
LOT_ID_ROLE = Qt.UserRole + 3
LINK_URL_ROLE = Qt.UserRole + 4  # the URL a link-button column should open
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
        on_edit_market_at_purchase=None,  # (lot_id, per_unit_cents) -> None
    ) -> None:
        super().__init__()
        self._rows: list[InventoryRow] = rows or []
        self._trends: dict[int, Trend] = {}  # item_id -> Trend (set async)
        self._on_edit_price = on_edit_price
        self._on_edit_intent = on_edit_intent
        self._on_edit_item = on_edit_item
        self._on_edit_market_at_purchase = on_edit_market_at_purchase

    def set_rows(self, rows: list[InventoryRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def set_trends(self, trends: dict[int, Trend]) -> None:
        """Attach per-item trends (computed off the UI thread) and repaint the
        Trend column in place — no model reset, so selection/scroll are kept."""
        self._trends = trends or {}
        if self._rows:
            top = self.index(0, TREND_COL)
            bottom = self.index(len(self._rows) - 1, TREND_COL)
            self.dataChanged.emit(
                top,
                bottom,
                [Qt.DisplayRole, Qt.ForegroundRole, Qt.ToolTipRole, Qt.UserRole],
            )

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
        if index.column() == TREND_COL:
            return self._trend_data(row, role)
        if col.link is not None:  # link-button column (TCGplayer / eBay)
            if role == LINK_URL_ROLE:
                if col.link == "tcgplayer":
                    return row.item.tcgplayer_url
                # eBay: a manual URL wins, else a derived sold-listings search so
                # every card has a working eBay link.
                return row.item.ebay_url or sold_search_url(row.item)
            if role == Qt.DisplayRole:
                return ""
            if role == Qt.UserRole:
                return col.sort_key(row)
            return None
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
            if index.column() == MARKET_AT_PURCHASE_COL:
                return (
                    "Market value recorded at purchase — click to edit for "
                    "corrections. Does not change cost basis or P/L."
                )
        if role == ITEM_ID_ROLE:
            return row.item.id
        if role == REVIEW_ROLE:
            return row.review
        if role == LOT_ID_ROLE:
            return row.lot_id
        if index.column() == MARKET_CURRENT_COL and role == Qt.EditRole:
            return (row.current_price_cents or 0) / 100.0
        if index.column() == MARKET_AT_PURCHASE_COL and role == Qt.EditRole:
            # Dollars as text → a plain line-edit editor (no spin-box range cap).
            return f"{row.market_at_purchase_cents / 100:.2f}"
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

    def _trend_data(self, row: InventoryRow, role):
        trend = self._trends.get(row.item.id)
        if role == Qt.DisplayRole:
            return _trend_text(trend)
        if role == Qt.UserRole:  # sort key
            return _trend_sort(trend)
        if role == Qt.ForegroundRole:
            return QBrush(_trend_color(trend))
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter)
        if role == Qt.ToolTipRole:
            return _trend_tooltip(trend)
        return None

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        col = index.column()
        row = self._rows[index.row()]
        if col == MARKET_CURRENT_COL:  # always editable → sets a manual price
            base |= Qt.ItemIsEditable
        elif col == MARKET_AT_PURCHASE_COL:  # editable acquisition snapshot
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
        if index.column() == MARKET_AT_PURCHASE_COL:
            try:
                per_unit_cents = round(float(value) * 100)
            except (TypeError, ValueError):
                return False
            if per_unit_cents < 0 or per_unit_cents == row.market_at_purchase_cents:
                return False  # invalid or unchanged → no write/refresh
            if self._on_edit_market_at_purchase is not None:
                self._on_edit_market_at_purchase(row.lot_id, per_unit_cents)
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
