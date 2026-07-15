"""Inventory view — sortable/filterable grid of holdings (F-VW-1/5/9, F-SE-1/2)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from PySide6.QtCharts import (
    QChart,
    QDateTimeAxis,
    QLineSeries,
    QValueAxis,
)
from PySide6.QtCore import (
    QDateTime,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import (
    CardCatalogRepository,
    ItemRepository,
    LotRepository,
    PriceSnapshotRepository,
)
from tcginventory.domain.enums import (
    Condition,
    Edition,
    HoloPattern,
    Intent,
    Language,
)
from tcginventory.domain.models import PriceSnapshot
from tcginventory.services import cardimages
from tcginventory.services.exceptions import ServiceError
from tcginventory.services.inventory import InventoryRow, InventoryService
from tcginventory.services.review import with_review_note, without_review_note
from tcginventory.services.transactions import TransactionService
from tcginventory.ui.card_preview import CardImagePane, install_hover_preview
from tcginventory.ui.charts import HoverChartView
from tcginventory.ui.models.inventory_model import (
    INTENT_COL,
    MARKET_CURRENT_COL,
    InventoryTableModel,
    combo_columns,
)
from tcginventory.ui.views._combo_delegate import ComboDelegate
from tcginventory.ui.views._intent_delegate import IntentDelegate
from tcginventory.ui.views._review_delegate import ReviewPriceDelegate
from tcginventory.ui.views.item_detail import ItemDetailDialog

_HISTORY_DAYS = 90

_INTENTS = ["All", "personal", "resale", "ripping", "mixed"]
_SOLD = ["All", "unsold", "partial", "sold"]
_GRADED = ["All", "Singles", "Graded"]


class _PreviewPrefetchWorker(QThread):
    """Pre-fetch, off the UI thread, everything a hover needs so it's instant:
    each row's card image (resolved + cached to disk) and each item's price
    history. Uses its own DB connection (sqlite handles aren't shareable across
    threads) and emits the image map ``{lot_id: (card_id, url)}`` and the history
    map ``{item_id: [PriceSnapshot]}``. The chart itself is still built on the UI
    thread (QtCharts isn't thread-safe), but from this cache — no DB round-trip.
    """

    resolved = Signal(dict, dict)

    def __init__(
        self, db_file: str, rows: list[tuple[int, int, str, str, str]], since: str
    ) -> None:
        super().__init__()
        self._db_file = db_file
        self._rows = rows
        self._since = since

    def run(self) -> None:  # pragma: no cover - exercised via the UI
        from tcginventory.data.db import connect

        images: dict[int, tuple[str, str]] = {}
        history: dict[int, list] = {}
        try:
            conn = connect(self._db_file)
            try:
                catalog = CardCatalogRepository(conn)
                snaps = PriceSnapshotRepository(conn)
                for lot_id, item_id, name, set_name, number in self._rows:
                    if self.isInterruptionRequested():
                        break
                    hit = catalog.image_for(name, set_name, number)
                    if hit:
                        images[lot_id] = hit
                        cardimages.ensure_cached(hit[0], hit[1])  # warm disk
                    if item_id not in history:
                        history[item_id] = snaps.history_for_item(
                            item_id, since=self._since
                        )
            finally:
                conn.close()
        except Exception:
            pass  # best-effort warming; never break the UI
        self.resolved.emit(images, history)


class _InventoryFilterProxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.setSortRole(Qt.UserRole)
        self._text = ""
        self._intent = "All"
        self._sold = "All"
        self._in_stock_only = False
        self._review_only = False
        self._graded = "All"  # All | Singles | Graded

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

    def set_review_only(self, on: bool) -> None:
        self._review_only = on
        self.invalidateFilter()

    def set_graded(self, mode: str) -> None:
        self._graded = mode
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        row = self.sourceModel().row_at(source_row)
        if self._graded == "Graded" and not row.item.is_graded:
            return False
        if self._graded == "Singles" and row.item.is_graded:
            return False
        if self._review_only and not row.review:
            return False
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
    changed = Signal()  # a hard-delete changed the ledger

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self._service = InventoryService(conn)
        # lot_id -> (card_id, image_url), populated in the background so hovers
        # don't re-query the catalog or wait on the network.
        self._row_image: dict[int, tuple[str, str]] = {}
        self._row_history: dict[int, list] = {}  # item_id -> price snapshots
        self._prefetch_worker: _PreviewPrefetchWorker | None = None
        self._columns_sized = False  # auto-size grid columns once, on first load
        self._model = InventoryTableModel(
            on_edit_price=self._set_manual_price,
            on_edit_intent=self._set_lot_intent,
            on_edit_item=self._set_item_field,
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

        self._graded = QComboBox()
        self._graded.addItems(_GRADED)
        self._graded.currentTextChanged.connect(self._proxy.set_graded)

        self._in_stock = QCheckBox("In stock only")
        self._in_stock.toggled.connect(self._proxy.set_in_stock_only)

        self._review_only = QCheckBox("Marked for review")
        self._review_only.setToolTip("Show only entries flagged for review")
        self._review_only.toggled.connect(self._proxy.set_review_only)

        self._delete_btn = QPushButton("Delete entry…")
        self._delete_btn.setToolTip(
            "Permanently delete the selected entry (for correcting a mistake)"
        )
        self._delete_btn.clicked.connect(self._delete_selected_entry)

        filter_bar = QHBoxLayout()
        filter_bar.addWidget(self._search, 1)
        filter_bar.addWidget(QLabel("Intent:"))
        filter_bar.addWidget(self._intent)
        filter_bar.addWidget(QLabel("Sold:"))
        filter_bar.addWidget(self._sold)
        filter_bar.addWidget(QLabel("Type:"))
        filter_bar.addWidget(self._graded)
        filter_bar.addWidget(self._in_stock)
        filter_bar.addWidget(self._review_only)
        filter_bar.addWidget(self._delete_btn)

        # -- table
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.AscendingOrder)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Single click into a cell opens its editor (see _edit_on_click);
        # double-click stays as a fallback and opens the dialog on read-only
        # columns.
        self._table.setEditTriggers(QAbstractItemView.DoubleClicked)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        # Notes is the last column → it stretches to absorb any leftover width.
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.doubleClicked.connect(self._open_detail)
        self._table.clicked.connect(self._edit_on_click)
        self._table.selectionModel().selectionChanged.connect(self._on_selection)
        # Hover a row to preview the card image in the pane by the chart
        # (lazily fetched + cached).
        self._preview_pane = CardImagePane()
        self._table.setMouseTracking(True)
        self._table.entered.connect(self._on_row_hover)
        install_hover_preview(
            self._table.viewport(), lambda: None, self._preview_pane
        )  # clear on leave

        # Review column: amber highlight + inline editor + two-stage button.
        self._review_delegate = ReviewPriceDelegate(
            self._table, self._unmark_and_refresh
        )
        self._table.setItemDelegateForColumn(MARKET_CURRENT_COL, self._review_delegate)

        # Intent column: edit inline with a drop-down (personal/resale/ripping).
        self._intent_delegate = IntentDelegate(self._table)
        self._table.setItemDelegateForColumn(INTENT_COL, self._intent_delegate)

        # Lang / Cond / Holo / Edition: inline drop-downs of their enum values.
        # (Name / Set / # / Notes use the default text editor.)
        self._combo_delegates = []
        for col, options in combo_columns():
            delegate = ComboDelegate(options, self._table)
            self._combo_delegates.append(delegate)
            self._table.setItemDelegateForColumn(col, delegate)

        self._empty = QLabel("Nothing here yet — record your first purchase.")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setVisible(False)

        # -- price-history chart (last ~3 months) for the selected card.
        # Rebuilding a QChart is heavy, so coalesce rapid hover/selection changes
        # and build it just after the pointer settles — this also keeps single-
        # click cell editing snappy (the click isn't blocked by a chart rebuild).
        self._chart_view = HoverChartView()
        self._chart_view.setMinimumHeight(220)
        self._pending_chart_row: InventoryRow | None = None
        self._chart_timer = QTimer(self)
        self._chart_timer.setSingleShot(True)
        self._chart_timer.setInterval(120)
        self._chart_timer.timeout.connect(
            lambda: self._set_history_chart(self._pending_chart_row)
        )
        self._show_chart_message("Select a card to see its price history.")

        splitter = QSplitter(Qt.Vertical)
        table_box = QWidget()
        table_layout = QVBoxLayout(table_box)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(self._table)
        table_layout.addWidget(self._empty)
        splitter.addWidget(table_box)

        # Bottom pane: card preview (left) beside the price-history chart.
        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        preview_box = QVBoxLayout()
        preview_box.addWidget(QLabel("Card preview"))
        preview_box.addWidget(self._preview_pane, 1)  # fills the column height
        bottom_layout.addLayout(preview_box, 1)
        bottom_layout.addWidget(self._chart_view, 4)
        splitter.addWidget(bottom)
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
        # Size columns only on first load — re-sizing on every edit is slow and
        # would also discard the user's column drags.
        if not self._columns_sized and rows:
            self._table.resizeColumnsToContents()
            self._apply_column_widths()
            self._columns_sized = True
        self._empty.setVisible(not rows)
        self._table.setVisible(bool(rows))
        self._prefetch_previews(rows)
        self._on_selection()

    def _col(self, header: str) -> int:
        return next(
            i
            for i in range(self._model.columnCount())
            if self._model.headerData(i, Qt.Horizontal) == header
        )

    def _apply_column_widths(self) -> None:
        """Widen the Holo / Edition / Intent drop-down columns so their editors
        have room; Notes (last column) stretches to absorb the leftover width."""
        for header, width in (("Holo", 70), ("Edition", 110), ("Intent", 110)):
            self._table.setColumnWidth(self._col(header), width)

    def _db_file(self) -> str | None:
        """The backing SQLite file, or None for an in-memory DB (tests)."""
        for _seq, name, file in self._conn.execute("PRAGMA database_list"):
            if name == "main":
                return file or None
        return None

    def _prefetch_previews(self, rows: list[InventoryRow]) -> None:
        """Warm the card image + price-history caches in the background so
        hovering the grid is instant (no per-hover catalog scan, network fetch,
        or history query)."""
        db_file = self._db_file()
        if not db_file or not rows:
            return
        if self._prefetch_worker is not None and self._prefetch_worker.isRunning():
            self._prefetch_worker.requestInterruption()
        payload = [
            (r.lot_id, r.item.id, r.item.name, r.item.set_name, r.item.card_number)
            for r in rows
        ]
        worker = _PreviewPrefetchWorker(db_file, payload, self._history_since())
        worker.resolved.connect(self._on_previews_resolved)
        self._prefetch_worker = worker
        worker.start()

    def _on_previews_resolved(self, images: dict, history: dict) -> None:
        self._row_image = images
        self._row_history = history

    @staticmethod
    def _history_since() -> str:
        return (
            datetime.now(timezone.utc) - timedelta(days=_HISTORY_DAYS)
        ).isoformat()

    def _selected_row(self) -> InventoryRow | None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        source = self._proxy.mapToSource(indexes[0])
        return self._model.row_at(source.row())

    def _on_selection(self, *_args) -> None:
        row = self._selected_row()
        self._review_delegate.note_selected_item(row.item.id if row else None)
        self._request_chart(row)

    def _request_chart(self, row: InventoryRow | None) -> None:
        """Debounce chart rebuilds (coalesces rapid hover/selection changes)."""
        self._pending_chart_row = row
        self._chart_timer.start()

    def _set_history_chart(self, row: InventoryRow | None) -> None:
        if row is None:
            self._show_chart_message("Select a card to see its price history.")
            return
        # Prefer the pre-fetched history; fall back to a live query only if the
        # background prefetch hasn't reached this item yet.
        history = self._row_history.get(row.item.id)
        if history is None:
            history = self._service.prices.history_for_item(
                row.item.id, since=self._history_since()
            )
        self._chart_view.setChart(_history_chart(row, history))

    def _on_row_hover(self, proxy_index: QModelIndex) -> None:
        row = self._model.row_at(self._proxy.mapToSource(proxy_index).row())
        if row is None:
            return
        # Card art (kept as the most-recently-viewed once the pointer leaves).
        # Prefer the pre-resolved map; fall back to a live lookup only if the
        # background prefetch hasn't reached this row yet.
        res = self._row_image.get(row.lot_id)
        if res is None:
            res = CardCatalogRepository(self._conn).image_for(
                row.item.name, row.item.set_name, row.item.card_number
            )
        self._preview_pane.show_card(*(res if res else (None, None)))
        # Price-history graph for the hovered row (also persists on leave).
        self._request_chart(row)

    def _show_chart_message(self, text: str) -> None:
        chart = QChart()
        chart.setTitle(text)
        self._chart_view.setChart(chart)

    def _open_detail(self, proxy_index: QModelIndex) -> None:
        source_index = self._proxy.mapToSource(proxy_index)
        row = self._model.row_at(source_index.row())
        # Inline-editable cells edit in place; only the read-only columns open
        # the full item editor dialog.
        if self._model.flags(source_index) & Qt.ItemIsEditable:
            return
        ItemDetailDialog(self._conn, row.item.id, parent=self).exec()
        self.refresh()  # reflect any edits made in the detail/editor

    def _edit_on_click(self, proxy_index: QModelIndex) -> None:
        """Open the cell's editor on a single click (except the Current Market
        cell, whose single click drives the review ✕/↻ button)."""
        if proxy_index.column() == MARKET_CURRENT_COL:
            return
        if self._model.flags(self._proxy.mapToSource(proxy_index)) & Qt.ItemIsEditable:
            self._table.edit(proxy_index)

    def _set_manual_price(self, item_id: int, per_unit_cents: int) -> None:
        """Inline-edit handler: store the manual per-unit price. Editing the
        price marks the item for review so the daily poll won't overwrite it."""
        item = ItemRepository(self._conn).get(item_id)
        if item is None:
            return
        item.market_value_cents = per_unit_cents
        item.market_value_asof = now_iso()[:10]
        item.review = True
        item.notes = with_review_note(item.notes)
        ItemRepository(self._conn).update(item)
        self._conn.commit()
        self.refresh()

    def _set_item_field(self, item_id: int, field: str, value) -> None:
        """Inline-edit handler for a card's descriptive/identity fields."""
        item = ItemRepository(self._conn).get(item_id)
        if item is None:
            return
        text = value.strip() if isinstance(value, str) else value
        if field == "name":
            item.name = text or item.name  # never blank the name
        elif field == "set_name":
            item.set_name = text or None
        elif field == "card_number":
            item.card_number = text or None
        elif field == "language":
            item.language = Language(text)
        elif field == "holo_pattern":
            item.holo_pattern = HoloPattern(text)
        elif field == "edition":
            item.edition = Edition(text)
        elif field == "notes":
            base = without_review_note(text)  # don't duplicate the marker
            item.notes = with_review_note(base) if item.review else base
        elif field == "condition":
            if item.is_graded:
                return  # graded cards have no condition to set
            item.condition = Condition(text)
            # Non-NM auto-marks for review; NM clears it (matches entry/editor).
            item.review = text != Condition.NM.value
            item.notes = (
                with_review_note(item.notes)
                if item.review
                else without_review_note(item.notes)
            )
        else:
            return
        ItemRepository(self._conn).update(item)
        self._conn.commit()
        self.refresh()

    def _delete_selected_entry(self) -> None:
        row = self._selected_row()
        if row is None:
            QMessageBox.information(
                self, "Delete entry", "Select an inventory entry to delete."
            )
            return
        label = f"{row.sku or row.item.name}"
        confirm = QMessageBox.warning(
            self,
            "Delete entry permanently",
            f"Permanently delete {label} ({row.item.name})?\n\n"
            "This removes the entry and its purchase line for good — it is not "
            "a void and cannot be undone. Use this only to fix a mistake.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            TransactionService(self._conn).delete_inventory_entry(row.lot_id)
        except ServiceError as exc:
            QMessageBox.warning(self, "Could not delete", str(exc))
            return
        self.refresh()
        self.changed.emit()

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
        item.notes = without_review_note(item.notes)
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
