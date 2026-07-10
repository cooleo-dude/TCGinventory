"""Inventory view — sortable/filterable grid of holdings (F-VW-1/5/9, F-SE-1/2)."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from tcginventory.services.inventory import InventoryService
from tcginventory.ui.models.inventory_model import InventoryTableModel
from tcginventory.ui.views.item_detail import ItemDetailDialog

_INTENTS = ["All", "personal", "resale", "ripping", "mixed"]
_SOLD = ["All", "unsold", "partial", "sold"]


class _InventoryFilterProxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.setSortRole(Qt.UserRole)
        self._text = ""
        self._intent = "All"
        self._sold = "All"
        self._in_stock_only = False

    def set_text(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def set_intent(self, intent: str) -> None:
        self._intent = intent
        self.invalidateFilter()

    def set_sold(self, sold: str) -> None:
        self._sold = sold
        self.invalidateFilter()

    def set_in_stock_only(self, on: bool) -> None:
        self._in_stock_only = on
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        row = self.sourceModel().row_at(source_row)
        if self._in_stock_only and not row.in_stock:
            return False
        if self._intent != "All" and row.intent != self._intent:
            return False
        if self._sold != "All" and row.sold_status != self._sold:
            return False
        if self._text:
            haystack = " ".join(
                filter(
                    None,
                    [row.item.name, row.item.set_name, row.item.card_number],
                )
            ).lower()
            if self._text not in haystack:
                return False
        return True


class InventoryView(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._service = InventoryService(conn)
        self._model = InventoryTableModel()
        self._proxy = _InventoryFilterProxy()
        self._proxy.setSourceModel(self._model)

        # -- filter bar
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search name / set / number…")
        self._search.textChanged.connect(self._proxy.set_text)

        self._intent = QComboBox()
        self._intent.addItems(_INTENTS)
        self._intent.currentTextChanged.connect(self._proxy.set_intent)

        self._sold = QComboBox()
        self._sold.addItems(_SOLD)
        self._sold.currentTextChanged.connect(self._proxy.set_sold)

        self._in_stock = QCheckBox("In stock only")
        self._in_stock.toggled.connect(self._proxy.set_in_stock_only)

        filter_bar = QHBoxLayout()
        filter_bar.addWidget(self._search, 1)
        filter_bar.addWidget(QLabel("Intent:"))
        filter_bar.addWidget(self._intent)
        filter_bar.addWidget(QLabel("Sold:"))
        filter_bar.addWidget(self._sold)
        filter_bar.addWidget(self._in_stock)

        # -- table
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.AscendingOrder)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.doubleClicked.connect(self._open_detail)

        self._empty = QLabel("Nothing here yet — record your first purchase.")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setVisible(False)

        layout = QVBoxLayout(self)
        layout.addLayout(filter_bar)
        layout.addWidget(self._table)
        layout.addWidget(self._empty)

        self.refresh()

    def refresh(self) -> None:
        rows = self._service.inventory_rows()
        self._model.set_rows(rows)
        self._table.resizeColumnsToContents()
        self._empty.setVisible(not rows)
        self._table.setVisible(bool(rows))

    def _open_detail(self, proxy_index: QModelIndex) -> None:
        source_index = self._proxy.mapToSource(proxy_index)
        row = self._model.row_at(source_index.row())
        ItemDetailDialog(self._conn, row.item.id, parent=self).exec()
        self.refresh()  # reflect any edits made in the detail/editor
