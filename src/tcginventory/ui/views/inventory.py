"""Inventory view — sortable/filterable grid of holdings (F-VW-1/5/9, F-SE-1/2)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

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
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QDesktopServices,
    QFont,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
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
from tcginventory.services.trends import TrendService
from tcginventory.ui import theme
from tcginventory.ui.card_preview import CardImagePane, install_hover_preview
from tcginventory.ui.charts import RangedChartView
from tcginventory.ui.formatting import money
from tcginventory.ui.models.inventory_model import (
    EBAY_COL,
    INTENT_COL,
    LINK_URL_ROLE,
    MARKET_CURRENT_COL,
    TCGPLAYER_COL,
    InventoryTableModel,
    combo_columns,
    subline_for,
)
from tcginventory.ui.views._combo_delegate import ComboDelegate
from tcginventory.ui.views._inventory_delegates import (
    BadgeDelegate,
    CardCellDelegate,
    IntentChipDelegate,
)
from tcginventory.ui.views._link_delegate import LinkButtonDelegate
from tcginventory.ui.views._review_delegate import ReviewPriceDelegate
from tcginventory.ui.views.item_detail import ItemDetailDialog

_INTENTS = ["All", "personal", "resale", "ripping", "mixed"]
_SOLD = ["All", "unsold", "partial", "sold"]
_GRADED = ["All", "Singles", "Graded"]


class _PreviewPrefetchWorker(QThread):
    """Pre-fetch, off the UI thread, everything a hover needs so it's instant:
    each row's card image (resolved + cached to disk), each item's price history,
    and each item's market **trend**. Uses its own DB connections (sqlite handles
    aren't shareable across threads) and exposes three maps as attributes:
    ``images`` ``{lot_id: (card_id, url)}``, ``history``
    ``{item_id: [PriceSnapshot]}``, and ``trends`` ``{item_id: Trend}``. The
    argless ``resolved`` signal just says "read them now" — dicts of Python
    objects don't survive marshalling across a queued cross-thread signal, so the
    results are handed over as attributes rather than signal arguments. The chart
    itself is still built on the UI thread (QtCharts isn't thread-safe), but from
    this cache — no DB round-trip.
    """

    resolved = Signal()

    def __init__(
        self,
        db_file: str,
        rows: list[tuple[int, int, str, str, str]],
        since: str | None,
    ) -> None:
        super().__init__()
        self._db_file = db_file
        self._rows = rows
        self._since = since
        self.images: dict[int, tuple[str, str]] = {}
        self.history: dict[int, list] = {}
        self.trends: dict[int, object] = {}

    def run(self) -> None:  # pragma: no cover - exercised via the UI
        from tcginventory.data.db import connect
        from tcginventory.data.pricehistory import open_history

        images: dict[int, tuple[str, str]] = {}
        history: dict[int, list] = {}
        trends: dict[int, object] = {}
        try:
            conn = connect(self._db_file)
            hist_conn = None
            try:
                try:
                    hist_conn = open_history()
                except Exception:
                    hist_conn = None  # no catalog history → snapshot fallback
                catalog = CardCatalogRepository(conn)
                snaps = PriceSnapshotRepository(conn)
                trend_svc = TrendService(conn, hist_conn)
                today = date.today()
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
                    if item_id not in trends:
                        item = SimpleNamespace(
                            id=item_id,
                            name=name,
                            set_name=set_name,
                            card_number=number,
                        )
                        trends[item_id] = trend_svc.trend_for(item, as_of=today)
            finally:
                conn.close()
                if hist_conn is not None:
                    hist_conn.close()
        except Exception:
            pass  # best-effort warming; never break the UI
        self.images, self.history, self.trends = images, history, trends
        self.resolved.emit()


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
        self._row_trends: dict[int, object] = {}  # item_id -> Trend
        self._thumb_cache: dict[int, QPixmap | None] = {}  # lot_id -> row thumbnail
        self._prefetch_worker: _PreviewPrefetchWorker | None = None
        self._columns_sized = False  # auto-size grid columns once, on first load
        self._model = InventoryTableModel(
            on_edit_price=self._set_manual_price,
            on_edit_intent=self._set_lot_intent,
            on_edit_item=self._set_item_field,
            on_edit_market_at_purchase=self._set_market_at_purchase,
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
        # The Card cell absorbs leftover width. (stretchLastSection would target
        # a link column, so stretch the Name/Card column explicitly.)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(
            self._col("Name"), QHeaderView.Stretch
        )
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
        # Right-click a row → mark/unmark as stolen (missing).
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_row_menu)

        # -- stylized delegates -------------------------------------------
        # Review column: amber highlight + inline editor + two-stage button.
        self._review_delegate = ReviewPriceDelegate(
            self._table, self._unmark_and_refresh
        )
        self._table.setItemDelegateForColumn(MARKET_CURRENT_COL, self._review_delegate)
        # Intent → coloured chip (keeps the inline personal/resale/ripping editor).
        self._intent_delegate = IntentChipDelegate(self._table)
        self._table.setItemDelegateForColumn(INTENT_COL, self._intent_delegate)
        # Name → composite Card cell: thumbnail + name + sold tag + sub-line
        # (set · # · holo · edition · notes). Edits open the detail dialog.
        self._card_delegate = CardCellDelegate(self._table, self._thumb_pixmap)
        self._card_delegate.setObjectName("cardCell")
        self._table.setItemDelegateForColumn(self._col("Name"), self._card_delegate)
        # Cond/Grade → badge (graded slabs tinted); other combos keep plain
        # editors even though they're hidden here (used elsewhere / on unhide).
        self._combo_delegates = []
        cond_col = self._col("Cond/Grade")
        for col, options in combo_columns():
            delegate = (
                BadgeDelegate(options, self._table)
                if col == cond_col
                else ComboDelegate(options, self._table)
            )
            self._combo_delegates.append(delegate)
            self._table.setItemDelegateForColumn(col, delegate)
        # TCGplayer / eBay link buttons, inline in the grid.
        self._tcg_delegate = LinkButtonDelegate(self._table, "TCGplayer")
        self._ebay_delegate = LinkButtonDelegate(self._table, "eBay")
        self._table.setItemDelegateForColumn(TCGPLAYER_COL, self._tcg_delegate)
        self._table.setItemDelegateForColumn(EBAY_COL, self._ebay_delegate)

        # Show only the curated columns; the rest fold into the Card cell and the
        # dossier (they stay in the model for sort/filter/dialog editing).
        visible = {
            "Name",
            "Cond/Grade",
            "Intent",
            "Acquired",
            "Purchase Price",
            "Current Market",
            "Trend",
            "Unrealized G/L",
            "Sold For",
            "TCGplayer",
            "eBay",
        }
        for c in range(self._model.columnCount()):
            header = self._model.headerData(c, Qt.Horizontal)
            self._table.setColumnHidden(c, header not in visible)
        # Taller rows so the two-line Card cell breathes.
        self._table.verticalHeader().setDefaultSectionSize(46)
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)

        # Clicking a link cell opens the listing (handled in _edit_on_click).
        self._table.clicked.connect(self._open_link)

        self._empty = QLabel("Nothing here yet — record your first purchase.")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setVisible(False)

        # -- price-history chart (last ~3 months) for the selected card.
        # Rebuilding a QChart is heavy, so coalesce rapid hover/selection changes
        # and build it just after the pointer settles — this also keeps single-
        # click cell editing snappy (the click isn't blocked by a chart rebuild).
        self._chart_view = RangedChartView(default_days=90)  # 3 Months by default
        self._chart_view.setMinimumHeight(180)
        self._pending_chart_row: InventoryRow | None = None
        self._chart_timer = QTimer(self)
        self._chart_timer.setSingleShot(True)
        self._chart_timer.setInterval(120)
        self._chart_timer.timeout.connect(
            lambda: self._set_history_chart(self._pending_chart_row)
        )
        self._show_chart_message("Select a card to see its price history.")

        # -- body: the grid (left) beside a card "dossier" (right).
        grid_box = QWidget()
        grid_layout = QVBoxLayout(grid_box)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(0)
        grid_layout.addWidget(self._table, 1)
        grid_layout.addWidget(self._empty)

        body = QSplitter(Qt.Horizontal)
        body.addWidget(grid_box)
        body.addWidget(self._build_dossier())
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 1)
        body.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addLayout(filter_bar)
        layout.addWidget(self._build_summary())
        layout.addWidget(body, 1)

        # Recompute the filter-scoped summary whenever the visible set changes.
        for sig in (
            self._search.textChanged,
            self._intent.currentTextChanged,
            self._sold.currentTextChanged,
            self._graded.currentTextChanged,
        ):
            sig.connect(lambda *_: self._update_summary())
        self._in_stock.toggled.connect(lambda *_: self._update_summary())
        self._review_only.toggled.connect(lambda *_: self._update_summary())

        self.refresh()

    def refresh(self) -> None:
        self._review_delegate.disarm()
        self._thumb_cache.clear()  # image map is about to be re-warmed
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
        self._update_summary()
        self._on_selection()

    def _col(self, header: str) -> int:
        return next(
            i
            for i in range(self._model.columnCount())
            if self._model.headerData(i, Qt.Horizontal) == header
        )

    def _apply_column_widths(self) -> None:
        """Give the chip/badge and link columns room; the Card (Name) column
        stretches to absorb whatever's left."""
        for header, width in (
            ("Cond/Grade", 88),
            ("Intent", 84),
            ("Trend", 52),
            ("Acquired", 88),
            ("Purchase Price", 96),
            ("Current Market", 104),
            ("Unrealized G/L", 100),
            ("Sold For", 82),
            ("TCGplayer", 90),
            ("eBay", 74),
        ):
            self._table.setColumnWidth(self._col(header), width)
        # Keep the Card (Name) column from collapsing under the stretch.
        self._table.horizontalHeader().setMinimumSectionSize(44)

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
        worker.resolved.connect(lambda w=worker: self._on_previews_resolved(w))
        self._prefetch_worker = worker
        worker.start()

    def _on_previews_resolved(self, worker: _PreviewPrefetchWorker) -> None:
        # Results are read from the finished worker's attributes, not signal args
        # (dicts of Python objects don't survive a cross-thread queued signal).
        self._row_image = worker.images
        self._row_history = worker.history
        self._row_trends = worker.trends
        self._model.set_trends(worker.trends)
        self._thumb_cache.clear()  # rebuild thumbs from the freshly-warmed images
        self._table.viewport().update()  # repaint Card cells with their art
        self._update_dossier(self._selected_row())  # fill the dossier art now

    @staticmethod
    def _history_since() -> str | None:
        # None → fetch all recorded history; the range selector zooms the view.
        return None

    def _selected_row(self) -> InventoryRow | None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        source = self._proxy.mapToSource(indexes[0])
        return self._model.row_at(source.row())

    def _on_selection(self, *_args) -> None:
        row = self._selected_row()
        self._review_delegate.note_selected_item(row.item.id if row else None)
        self._update_dossier(row)
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

    def _open_link(self, proxy_index: QModelIndex) -> None:
        """Open a card's TCGplayer/eBay listing when its link cell is clicked."""
        if proxy_index.column() not in (TCGPLAYER_COL, EBAY_COL):
            return
        url = proxy_index.data(LINK_URL_ROLE)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _show_row_menu(self, pos) -> None:
        """Right-click menu on an inventory row: mark/unmark as stolen."""
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        row = self._model.row_at(self._proxy.mapToSource(index).row())
        menu = QMenu(self._table)
        if row.missing:
            act = menu.addAction("Unmark stolen (restore)")
        else:
            act = menu.addAction("Mark as stolen / missing")
        act.triggered.connect(lambda: self._toggle_missing(row))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _toggle_missing(self, row: InventoryRow) -> None:
        """Flag/unflag the row's lot as stolen — a reversible $0 write-down."""
        if not self._service.set_lot_missing(row.lot_id, not row.missing):
            return
        self._conn.commit()
        self.refresh()
        self.changed.emit()  # portfolio totals moved → refresh dashboard

    def _open_detail(self, proxy_index: QModelIndex) -> None:
        source_index = self._proxy.mapToSource(proxy_index)
        row = self._model.row_at(source_index.row())
        # The Card (Name) cell and any read-only column open the full item
        # editor; the other inline-editable cells edit in place.
        name_col = self._col("Name")
        editable = bool(self._model.flags(source_index) & Qt.ItemIsEditable)
        if editable and source_index.column() != name_col:
            return
        ItemDetailDialog(self._conn, row.item.id, parent=self).exec()
        self.refresh()  # reflect any edits made in the detail/editor

    def _edit_on_click(self, proxy_index: QModelIndex) -> None:
        """Open the cell's editor on a single click. Skipped for: the Current
        Market cell (its click drives the review ✕/↻ button), the link columns
        (they open the listing), and the Card cell (it edits via the dialog)."""
        col = proxy_index.column()
        if col in (MARKET_CURRENT_COL, TCGPLAYER_COL, EBAY_COL, self._col("Name")):
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

    def _set_market_at_purchase(self, lot_id: int, per_unit_cents: int) -> None:
        """Inline-edit handler: correct a lot's market-value-at-purchase snapshot.

        A record fix only — it does not re-run cost allocation, so cost basis and
        P/L are untouched (see spec 02: this field is the acquisition snapshot,
        not a valuation input)."""
        lots = LotRepository(self._conn)
        lot = lots.get(lot_id)
        if lot is None or per_unit_cents < 0:
            return
        lot.acquired_market_value_cents = per_unit_cents
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

    # -- summary strip ----------------------------------------------------

    @staticmethod
    def _mono(size: float, bold: bool = False) -> QFont:
        f = QFont(theme.mono_family())
        f.setPointSizeF(size)
        f.setBold(bold)
        return f

    def _build_summary(self) -> QWidget:
        """A filter-scoped totals strip: On hand · Unique · Cost · Market ·
        Unrealized, reflecting whatever the filter bar currently shows."""
        t = theme.active()
        self._sum: dict[str, QLabel] = {}
        strip = QWidget()
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        tiles = [
            ("onhand", "On hand", False),
            ("unique", "Unique cards", False),
            ("cost", "Cost basis", False),
            ("market", "Market value", True),
            ("unrealized", "Unrealized", False),
        ]
        for key, label, hero in tiles:
            tile = QFrame()
            tile.setObjectName("sumtile")
            tv = QVBoxLayout(tile)
            tv.setContentsMargins(13, 7, 15, 8)
            tv.setSpacing(1)
            cap = QLabel(label.upper())
            cap.setStyleSheet(
                f"color:{t.ink3};font-size:10px;font-weight:700;"
                "letter-spacing:.6px;background:transparent;border:none;"
            )
            val = QLabel("—")
            val.setFont(self._mono(15, bold=True))
            val.setStyleSheet(f"color:{t.ink};background:transparent;border:none;")
            tv.addWidget(cap)
            tv.addWidget(val)
            rail = f"border-left:3px solid {t.accent};" if hero else ""
            tile.setStyleSheet(
                f"QFrame#sumtile{{background:{t.surface};border:1px solid "
                f"{t.line};border-radius:11px;{rail}}}"
            )
            self._sum[key] = val
            row.addWidget(tile)
        row.addStretch(1)
        return strip

    def _update_summary(self) -> None:
        """Recompute the strip over the rows the proxy currently accepts."""
        if not hasattr(self, "_sum"):
            return
        on_hand = cost = market = 0
        unique: set[int] = set()
        for i in range(self._proxy.rowCount()):
            src = self._proxy.mapToSource(self._proxy.index(i, 0))
            row = self._model.row_at(src.row())
            unique.add(row.item.id)
            held = row.on_hand if row.in_stock else 0
            if held <= 0:
                continue
            on_hand += held
            cost += held * row.purchase_price_cents
            if row.current_price_cents is not None:
                market += held * row.current_price_cents
        unreal = market - cost
        t = theme.active()
        self._sum["onhand"].setText(f"{on_hand:,}")
        self._sum["unique"].setText(f"{len(unique):,}")
        self._sum["cost"].setText(money(cost))
        self._sum["market"].setText(money(market))
        gl = self._sum["unrealized"]
        gl.setText(f"{'+' if unreal >= 0 else '−'}{money(abs(unreal))}")
        gl.setStyleSheet(
            f"color:{t.pos if unreal >= 0 else t.neg};"
            "background:transparent;border:none;"
        )

    # -- card dossier -----------------------------------------------------

    _FACTS = [
        ("sku", "SKU"),
        ("acquired", "Acquired"),
        ("cost", "Cost / unit"),
        ("market", "Market / unit"),
        ("unrealized", "Unrealized / unit"),
        ("sold", "Sold for"),
    ]

    def _build_dossier(self) -> QWidget:
        t = theme.active()
        panel = QFrame()
        panel.setObjectName("dossier")
        panel.setStyleSheet(
            f"QFrame#dossier{{background:{t.surface};border:1px solid {t.line};"
            "border-radius:12px;}"
        )
        panel.setMinimumWidth(288)
        panel.setMaximumWidth(430)
        v = QVBoxLayout(panel)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(9)

        cap = QLabel("SELECTED CARD")
        cap.setStyleSheet(
            f"color:{t.ink3};font-size:10px;font-weight:700;letter-spacing:2px;"
        )
        v.addWidget(cap)

        self._dossier_name = QLabel("—")
        self._dossier_name.setStyleSheet(
            f"color:{t.ink};font-size:15px;font-weight:700;"
        )
        self._dossier_name.setWordWrap(True)
        v.addWidget(self._dossier_name)

        self._dossier_sub = QLabel("")
        self._dossier_sub.setStyleSheet(f"color:{t.ink3};font-size:11px;")
        self._dossier_sub.setWordWrap(True)
        v.addWidget(self._dossier_sub)

        # Fixed art region (the pane's own policy is Ignored/Ignored, so reserve
        # its height explicitly or the layout collapses it).
        self._preview_pane.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._preview_pane.setFixedHeight(230)
        v.addWidget(self._preview_pane, 0, Qt.AlignHCenter)

        facts = QGridLayout()
        facts.setContentsMargins(0, 2, 0, 2)
        facts.setHorizontalSpacing(10)
        facts.setVerticalSpacing(5)
        self._fact_vals: dict[str, QLabel] = {}
        for i, (key, label) in enumerate(self._FACTS):
            k = QLabel(label)
            k.setStyleSheet(f"color:{t.ink3};font-size:11px;")
            val = QLabel("—")
            val.setFont(self._mono(11.5))
            val.setStyleSheet(f"color:{t.ink2};")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            facts.addWidget(k, i, 0)
            facts.addWidget(val, i, 1)
            self._fact_vals[key] = val
        facts.setColumnStretch(0, 1)
        v.addLayout(facts)

        v.addWidget(self._chart_view, 1)
        return panel

    def _update_dossier(self, row: InventoryRow | None) -> None:
        if not hasattr(self, "_fact_vals"):
            return
        t = theme.active()
        if row is None:
            self._dossier_name.setText("—")
            self._dossier_sub.setText("Select a card to see its details.")
            self._preview_pane.show_card(None, None)
            for val in self._fact_vals.values():
                val.setText("—")
                val.setStyleSheet(f"color:{t.ink2};")
            return
        self._dossier_name.setText(row.item.name)
        self._dossier_sub.setText(subline_for(row))
        # Only show art the background prefetch has already cached to disk —
        # never let a selection kick off its own network fetch (that would leak a
        # worker thread and isn't needed: the prefetch warms the same file).
        res = self._row_image.get(row.lot_id)
        if res and cardimages.cache_path(res[0]).exists():
            self._preview_pane.show_card(*res)

        self._fact_vals["sku"].setText(row.sku or "—")
        self._fact_vals["acquired"].setText(row.acquired_at or "—")
        self._fact_vals["cost"].setText(money(row.purchase_price_cents))
        cur = row.current_price_cents
        self._fact_vals["market"].setText(money(cur) if cur is not None else "—")
        unreal = row.unrealized_cents
        gl = self._fact_vals["unrealized"]
        if unreal is None:
            gl.setText("—")
            gl.setStyleSheet(f"color:{t.ink2};")
        else:
            gl.setText(f"{'+' if unreal >= 0 else '−'}{money(abs(unreal))}")
            gl.setStyleSheet(f"color:{t.pos if unreal >= 0 else t.neg};")
        sold = self._fact_vals["sold"]
        if row.sold_price_cents is not None:
            sold.setText(money(row.sold_price_cents))
            sold.setStyleSheet(f"color:{t.ink2};")
        else:
            sold.setText("—")
            sold.setStyleSheet(f"color:{t.ink3};")

    # -- row thumbnails ---------------------------------------------------

    def _thumb_pixmap(self, lot_id: int) -> QPixmap | None:
        """A small rounded thumbnail for the Card cell, cached per lot. Returns
        None until the background prefetch has warmed this lot's image."""
        if lot_id in self._thumb_cache:
            return self._thumb_cache[lot_id]
        pm: QPixmap | None = None
        res = self._row_image.get(lot_id)
        if res:
            path = cardimages.cache_path(res[0])
            if path.exists():
                src = QPixmap(str(path))
                if not src.isNull():
                    pm = self._round_thumb(src)
        self._thumb_cache[lot_id] = pm
        return pm

    @staticmethod
    def _round_thumb(src: QPixmap, w: int = 26, h: int = 36) -> QPixmap:
        """Centre-crop ``src`` to a rounded w×h thumbnail (device-pixel aware)."""
        ratio = 2
        tw, th = w * ratio, h * ratio
        scaled = src.scaled(
            tw, th, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
        )
        out = QPixmap(tw, th)
        out.fill(Qt.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, tw, th, 8, 8)
        painter.setClipPath(clip)
        x = (tw - scaled.width()) // 2
        y = (th - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
        painter.end()
        out.setDevicePixelRatio(ratio)
        return out


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
    chart.setTitle(f"{name} — market price")

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
