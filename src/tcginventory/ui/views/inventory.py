"""Inventory view — sortable/filterable grid of holdings (F-VW-1/5/9, F-SE-1/2)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from PySide6.QtCharts import (
    QChart,
    QChartView,
    QDateTimeAxis,
    QLineSeries,
    QValueAxis,
)
from PySide6.QtCore import QDateTime, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import (
    ItemRepository,
    LotRepository,
    PriceSnapshotRepository,
)
from tcginventory.domain.enums import Intent
from tcginventory.domain.models import PriceSnapshot
from tcginventory.services.inventory import InventoryRow, InventoryService
from tcginventory.ui.models.inventory_model import (
    INTENT_COL,
    MARKET_CURRENT_COL,
    InventoryTableModel,
)
from tcginventory.ui.views._intent_delegate import IntentDelegate
from tcginventory.ui.views._review_delegate import ReviewPriceDelegate
from tcginventory.ui.views.item_detail import ItemDetailDialog

_HISTORY_DAYS = 90

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
                    [row.item.name, row.item.set_name, row.item.card_number, row.sku],
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
        self._model = InventoryTableModel(
            on_edit_price=self._set_manual_price,
            on_edit_intent=self._set_lot_intent,
        )
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
        self._table.setEditTriggers(QAbstractItemView.DoubleClicked)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.doubleClicked.connect(self._open_detail)
        self._table.selectionModel().selectionChanged.connect(self._on_selection)

        # Review column: amber highlight + inline editor + two-stage button.
        self._review_delegate = ReviewPriceDelegate(
            self._table, self._unmark_and_refresh
        )
        self._table.setItemDelegateForColumn(MARKET_CURRENT_COL, self._review_delegate)

        # Intent column: edit inline with a drop-down (personal/resale/ripping).
        self._intent_delegate = IntentDelegate(self._table)
        self._table.setItemDelegateForColumn(INTENT_COL, self._intent_delegate)

        self._empty = QLabel("Nothing here yet — record your first purchase.")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setVisible(False)

        # -- price-history chart (last ~3 months) for the selected card
        self._chart_view = QChartView()
        self._chart_view.setRenderHint(QPainter.Antialiasing)
        self._chart_view.setMinimumHeight(220)
        self._show_chart_message("Select a card to see its price history.")

        splitter = QSplitter(Qt.Vertical)
        table_box = QWidget()
        table_layout = QVBoxLayout(table_box)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(self._table)
        table_layout.addWidget(self._empty)
        splitter.addWidget(table_box)
        splitter.addWidget(self._chart_view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(filter_bar)
        layout.addWidget(splitter)

        self.refresh()

    def refresh(self) -> None:
        self._review_delegate.disarm()
        rows = self._service.inventory_rows()
        self._model.set_rows(rows)
        self._table.resizeColumnsToContents()
        self._empty.setVisible(not rows)
        self._table.setVisible(bool(rows))
        self._on_selection()

    def _selected_row(self) -> InventoryRow | None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        source = self._proxy.mapToSource(indexes[0])
        return self._model.row_at(source.row())

    def _on_selection(self, *_args) -> None:
        row = self._selected_row()
        self._review_delegate.note_selected_item(row.item.id if row else None)
        if row is None:
            self._show_chart_message("Select a card to see its price history.")
            return
        since = (datetime.now(timezone.utc) - timedelta(days=_HISTORY_DAYS)).isoformat()
        history = self._service.prices.history_for_item(row.item.id, since=since)
        self._chart_view.setChart(_history_chart(row, history))

    def _show_chart_message(self, text: str) -> None:
        chart = QChart()
        chart.setTitle(text)
        self._chart_view.setChart(chart)

    def _open_detail(self, proxy_index: QModelIndex) -> None:
        source_index = self._proxy.mapToSource(proxy_index)
        row = self._model.row_at(source_index.row())
        # Double-clicking an inline-editable cell edits it instead of opening
        # the detail dialog (marked price cell, or the Intent drop-down).
        if proxy_index.column() == MARKET_CURRENT_COL and row.review:
            return
        if proxy_index.column() == INTENT_COL:
            return
        ItemDetailDialog(self._conn, row.item.id, parent=self).exec()
        self.refresh()  # reflect any edits made in the detail/editor

    def _set_manual_price(self, item_id: int, per_unit_cents: int) -> None:
        """Inline-edit handler: store the manual per-unit price (stays marked)."""
        item = ItemRepository(self._conn).get(item_id)
        if item is None:
            return
        item.market_value_cents = per_unit_cents
        item.market_value_asof = now_iso()[:10]
        ItemRepository(self._conn).update(item)
        self._conn.commit()
        self.refresh()

    def _set_lot_intent(self, lot_id: int, intent_str: str) -> None:
        """Inline-edit handler: change a single lot's intent (per-lot, Spec 02)."""
        lots = LotRepository(self._conn)
        lot = lots.get(lot_id)
        if lot is None:
            return
        new_intent = Intent(intent_str)
        if lot.intent is new_intent:
            return
        lot.intent = new_intent
        lots.update(lot)
        self._conn.commit()
        self.refresh()

    def _unmark_and_refresh(self, item_id: int) -> None:
        """Second-click handler: unmark and adopt the latest polled price."""
        item = ItemRepository(self._conn).get(item_id)
        if item is None:
            return
        latest = PriceSnapshotRepository(self._conn).latest_prices().get(item_id)
        if latest is not None:
            item.market_value_cents = latest
            item.market_value_asof = now_iso()[:10]
        item.review = False
        ItemRepository(self._conn).update(item)
        self._conn.commit()
        self.refresh()


def _to_msecs(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = datetime.fromisoformat(iso[:10])
    return dt.timestamp() * 1000.0


def _history_chart(row: InventoryRow, snapshots: list[PriceSnapshot]) -> QChart:
    chart = QChart()
    name = row.item.name
    if not snapshots:
        chart.setTitle(f"{name} — no price history yet (use File ▸ Refresh prices)")
        return chart
    chart.setTitle(f"{name} — market price, last {_HISTORY_DAYS} days")

    price = QLineSeries()
    price.setName("Market price")
    price.setPointsVisible(True)
    xs = []
    for snap in snapshots:
        x = _to_msecs(snap.as_of)
        xs.append(x)
        price.append(x, snap.price_cents / 100.0)
    chart.addSeries(price)

    # Flat per-unit reference lines across the same date span.
    x0, x1 = min(xs), max(xs)
    flat_values = [
        ("Cost", row.purchase_price_cents / 100.0),
        ("Market at ToP", row.market_at_purchase_cents / 100.0),
    ]
    flat_series = []
    for label, value in flat_values:
        line = QLineSeries()
        line.setName(label)
        line.append(x0, value)
        line.append(x1, value)
        chart.addSeries(line)
        flat_series.append(line)

    axis_x = QDateTimeAxis()
    axis_x.setFormat("MMM d")
    axis_x.setTitleText("Date")
    chart.addAxis(axis_x, Qt.AlignBottom)
    axis_y = QValueAxis()
    axis_y.setTitleText("USD")
    chart.addAxis(axis_y, Qt.AlignLeft)
    for series in (price, *flat_series):
        series.attachAxis(axis_x)
        series.attachAxis(axis_y)

    ys = [s.price_cents / 100.0 for s in snapshots] + [v for _, v in flat_values]
    axis_x.setRange(
        QDateTime.fromMSecsSinceEpoch(int(min(xs))),
        QDateTime.fromMSecsSinceEpoch(int(max(xs))),
    )
    low, high = min(ys), max(ys)
    pad = max(1.0, (high - low) * 0.1)
    axis_y.setRange(low - pad, high + pad)
    return chart
