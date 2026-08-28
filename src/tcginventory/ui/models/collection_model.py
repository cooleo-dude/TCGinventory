"""The Personal Collection checklist as a Qt model, shared by both views.

The grid and the list are two presentations of **one** ordered, filtered list of
slots. Putting the order and the filter here rather than in either view is what
stops them disagreeing: a chip that hides missing slots hides them in both, and a
sort applies to the pages the grid draws as well as the rows the table shows.

Follows ``ui/models/inventory_model.py``: a table model plus a
``QSortFilterProxyModel``, with sorting done on a dedicated role so a column can
sort by something other than the text it displays — collector numbers must run
1, 2, 10, 100 rather than lexicographically.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from tcginventory.services.collections import (
    FILLED,
    IMPORTABLE,
    MISSING,
    Slot,
    collector_key,
)
from tcginventory.ui.formatting import money

_ROOT = QModelIndex()  # module-level singleton for default parent args,
# matching ui/models/inventory_model.py

NUMBER_COL, CARD_COL, SET_COL, ARTIST_COL, RARITY_COL = range(5)
PRINTING_COL, STATE_COL, VALUE_COL = range(5, 8)
COLUMNS = (
    "No.",
    "Card",
    "Set",
    "Artist",
    "Rarity",
    "Printing",
    "State",
    "Value",
)

# What a slot is, for the grid delegate and for filtering — the row's Slot, and a
# sort key that is not the display string.
SLOT_ROLE = Qt.UserRole + 1
SORT_ROLE = Qt.UserRole + 2

# Same spellings the Inventory grid uses for its Card sub-line, so a card reads
# the same on both screens.
_HOLO_LABEL = {"H": "Holo", "RH": "Reverse Holo"}


def slot_subline(slot: Slot) -> str:
    """A slot's secondary line: ``set · number · holo``.

    Mirrors ``inventory_model.subline_for``, including its omissions: a plain
    non-holo card says nothing about foil, because "NH" on nine cards in ten is
    noise rather than information.

    In master mode the printing is already the slot's identity, so `treatment`
    is what gets shown; by number it is not, so the *held* copy's foil is — which
    is the only way a by-number checklist can say "you have the reverse holo".
    """
    parts: list[str] = []
    if slot.set_name:
        parts.append(slot.set_name)
    if slot.number:
        parts.append(slot.number)
    if slot.treatment:
        parts.append(slot.treatment)
    elif slot.holo and slot.holo != "NH":
        parts.append(_HOLO_LABEL.get(slot.holo, slot.holo))
    elif slot.only_printing and slot.only_printing != "Normal":
        # By number, a card that comes only one way *is* that printing — which
        # is how Base Set 1-16 read as holos without anyone owning them. "Normal"
        # is omitted for the same reason "NH" is: it is the default, not news.
        parts.append(slot.only_printing)
    return " · ".join(parts)


_STATE_LABEL = {
    FILLED: "In collection",
    IMPORTABLE: "Owned · resale",
    MISSING: "Not owned",
}

# The filter chips. "duplicates" means a filled slot holding more than one copy —
# the spares a collector would trade away.
FILTER_ALL = "all"
FILTER_FILLED = "filled"
FILTER_IMPORTABLE = "importable"
FILTER_MISSING = "missing"
FILTER_DUPLICATES = "duplicates"


class CollectionSlotModel(QAbstractTableModel):
    """One row per slot. Rebuilt wholesale when the collection changes."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._slots: list[Slot] = []

    def set_slots(self, slots: list[Slot]) -> None:
        self.beginResetModel()
        self._slots = list(slots)
        self.endResetModel()

    def slot_at(self, row: int) -> Slot | None:
        return self._slots[row] if 0 <= row < len(self._slots) else None

    # -- QAbstractTableModel -----------------------------------------------

    def rowCount(self, parent: QModelIndex = _ROOT) -> int:  # noqa: N802 (Qt override)
        return 0 if parent.isValid() else len(self._slots)

    def columnCount(self, parent: QModelIndex = _ROOT) -> int:  # noqa: N802 (Qt override)
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(  # noqa: N802 (Qt override)
        self, section: int, orientation, role=Qt.DisplayRole
    ):
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return COLUMNS[section] if 0 <= section < len(COLUMNS) else None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        slot = self._slots[index.row()]
        if role == SLOT_ROLE:
            return slot
        if role == SORT_ROLE:
            return self._sort_key(slot, index.column())
        if role == Qt.TextAlignmentRole and index.column() == VALUE_COL:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return self._text(slot, index.column())
        return None

    @staticmethod
    def _text(slot: Slot, column: int) -> str:
        if column == NUMBER_COL:
            return slot.number or "?"
        if column == CARD_COL:
            return slot.name or ""
        if column == SET_COL:
            return slot.set_name or ""
        if column == ARTIST_COL:
            return slot.artist or ""
        if column == RARITY_COL:
            return slot.rarity or ""
        if column == PRINTING_COL:
            if slot.treatment:
                return slot.treatment
            # Blank and "unknown" are different facts: no printing was asked for,
            # versus asked for and not known. Never let them look the same.
            return "unknown" if slot.treatments_unknown else ""
        if column == STATE_COL:
            label = _STATE_LABEL.get(slot.state, slot.state)
            return f"{label} ×{slot.qty}" if slot.qty > 1 else label
        if column == VALUE_COL:
            return money(slot.market_value_cents) if slot.market_value_cents else ""
        return ""

    def _sort_key(self, slot: Slot, column: int):
        """Sort by the underlying value, not the rendered string.

        Two columns would sort wrongly on their text: collector numbers
        ("10" before "2") and money ("$9.00" after "$100.00").
        """
        if column == NUMBER_COL:
            return collector_key(slot.number)
        if column == VALUE_COL:
            return slot.market_value_cents or -1
        if column == STATE_COL:
            # Group by usefulness rather than alphabetically: what is in the
            # collection, then what could be, then what is not owned.
            order = {FILLED: 0, IMPORTABLE: 1, MISSING: 2}
            return (order.get(slot.state, 3), -slot.qty)
        return (self._text(slot, column) or "").lower()


class CollectionFilterProxy(QSortFilterProxyModel):
    """The shared order and filter. Both views read through this."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = FILTER_ALL
        self.setSortRole(SORT_ROLE)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._text = ""

    def set_state_filter(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.invalidateRowsFilter()

    def state_filter(self) -> str:
        return self._state

    def set_text_filter(self, text: str) -> None:
        text = (text or "").strip().lower()
        if text == self._text:
            return
        self._text = text
        self.invalidateRowsFilter()

    def lessThan(  # noqa: N802 (Qt override)
        self, left: QModelIndex, right: QModelIndex
    ) -> bool:
        """Order by the sort role, compared in Python.

        Qt's default ``lessThan`` compares the role's value **as a QVariant**, and
        the number column's key is a Python tuple (letter head, number, tail) —
        which a QVariant cannot order, so the sort silently did nothing and the
        grid stayed in source order however the header was clicked. Comparing
        here keeps the tuple a tuple.
        """
        a = left.data(SORT_ROLE)
        b = right.data(SORT_ROLE)
        if a is None or b is None:
            return a is None and b is not None
        try:
            return a < b
        except TypeError:
            # Mixed types in one column should be impossible, but a crash inside
            # a sort comparator takes the window with it. Fall back to text.
            return str(a) < str(b)

    def filterAcceptsRow(  # noqa: N802 (Qt override)
        self, row: int, parent: QModelIndex
    ) -> bool:
        model = self.sourceModel()
        if model is None:
            return True
        slot = model.slot_at(row)
        if slot is None:
            return False
        if not self._accepts_state(slot):
            return False
        if self._text:
            haystack = " ".join(
                part
                for part in (
                    slot.name,
                    slot.number,
                    slot.set_name,
                    slot.artist,
                    slot.rarity,
                    slot.treatment,
                )
                if part
            ).lower()
            if self._text not in haystack:
                return False
        return True

    def _accepts_state(self, slot: Slot) -> bool:
        if self._state == FILTER_ALL:
            return True
        if self._state == FILTER_DUPLICATES:
            return slot.state == FILLED and slot.qty > 1
        return slot.state == self._state

    # -- convenience for the paginated grid --------------------------------

    def slot_at_row(self, row: int) -> Slot | None:
        """The slot shown at a *proxy* row — i.e. after sorting and filtering."""
        index = self.index(row, 0)
        return index.data(SLOT_ROLE) if index.isValid() else None

    def visible_slots(self, start: int = 0, count: int | None = None) -> list[Slot]:
        """A window of the visible, ordered slots — what one grid page draws."""
        total = self.rowCount()
        stop = total if count is None else min(total, start + count)
        out: list[Slot] = []
        for row in range(max(0, start), stop):
            slot = self.slot_at_row(row)
            if slot is not None:
                out.append(slot)
        return out
