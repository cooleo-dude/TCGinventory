"""Inventory view — sortable/filterable grid of holdings (F-VW-1/5/9, F-SE-1/2)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

from PySide6.QtCharts import (
    QAreaSeries,
    QChart,
    QDateTimeAxis,
    QLineSeries,
    QValueAxis,
)
from PySide6.QtCore import (
    QEvent,
    QMargins,
    QModelIndex,
    QRectF,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QGradient,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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
from tcginventory.services import cardimages
from tcginventory.services.exceptions import ServiceError
from tcginventory.services.inventory import InventoryRow, InventoryService
from tcginventory.services.review import with_review_note, without_review_note
from tcginventory.services.transactions import TransactionService
from tcginventory.services.trends import TrendService
from tcginventory.ui import icons, theme
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
from tcginventory.ui.views._voice_input import VoiceInputButton
from tcginventory.ui.views._worker import retire
from tcginventory.ui.views.item_editor import ItemEditorDialog


class _Segmented(QWidget):
    """A pill-styled segmented control: a row of exclusive buttons. Emits the
    selected *value* (not the label) via ``changed``."""

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("segwrap")
        t = theme.active()
        self.setStyleSheet(
            f"QWidget#segwrap{{background:{t.surface};border:1px solid {t.line2};"
            f"border-radius:9px;}}"
            f"QWidget#segwrap QPushButton{{background:transparent;border:none;"
            f"color:{t.ink2};padding:5px 12px;border-radius:6px;font-weight:600;"
            f"font-size:12px;}}"
            f"QWidget#segwrap QPushButton:hover{{color:{t.ink};}}"
            f"QWidget#segwrap QPushButton:checked{{background:{t.accent_soft};"
            f"color:{t.accent_ink};}}"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for i, (label, value) in enumerate(options):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=value: self.changed.emit(v))
            self._group.addButton(b)
            lay.addWidget(b)
            self._buttons[value] = b
            if i == 0:
                b.setChecked(True)

    def select(self, value: str) -> None:
        """Programmatically choose ``value`` (checks it and emits ``changed``)."""
        button = self._buttons.get(value)
        if button is not None:
            button.setChecked(True)
            self.changed.emit(value)


class _Chip(QPushButton):
    """A rounded, checkable filter chip (accent-tinted when on)."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("chip")
        t = theme.active()
        self.setStyleSheet(
            f"QPushButton#chip{{background:{t.surface};border:1px solid {t.line2};"
            f"border-radius:999px;color:{t.ink2};padding:5px 13px;font-weight:600;"
            f"font-size:12px;}}"
            f"QPushButton#chip:hover{{color:{t.ink};}}"
            f"QPushButton#chip:checked{{background:{t.accent_soft};"
            f"border-color:{t.accent};color:{t.accent_ink};}}"
        )


class _DossierArt(QWidget):
    """The dossier's card preview: paints the card image at a **fixed card
    aspect**, centred and filling the region — so every card reads at the same
    size (no per-image height jumps) — with rounded corners and no frame/box."""

    _RATIO = 342 / 245  # card height / width

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pm: QPixmap | None = None
        self.setMinimumHeight(280)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)

    def set_pixmap(self, pm: QPixmap | None) -> None:
        self._pm = pm if (pm is not None and not pm.isNull()) else None
        self.update()

    def paintEvent(self, _event) -> None:
        t = theme.active()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        cw = min(w, h / self._RATIO)
        ch = cw * self._RATIO
        x = (w - cw) / 2
        y = (h - ch) / 2
        rect = QRectF(x, y, cw, ch)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        if self._pm is not None:
            p.setClipPath(path)
            scaled = self._pm.scaled(
                int(round(cw)),
                int(round(ch)),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            sx = x + (cw - scaled.width()) / 2
            sy = y + (ch - scaled.height()) / 2
            p.drawPixmap(int(sx), int(sy), scaled)
        else:
            p.setPen(QColor(t.ink3))
            font = QFont(self.font())
            font.setPointSizeF(10.5)
            p.setFont(font)
            p.drawText(self.rect(), int(Qt.AlignCenter), "No preview")
        p.end()


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

    def warm_rows(self, conn, snaps, trend_svc, today):
        """Resolve art, price history and trend per row; returns the three maps.

        Split out of :meth:`run` so the per-row isolation is testable without a
        thread or a network.
        """
        images: dict[int, tuple[str, str]] = {}
        history: dict[int, list] = {}
        trends: dict[int, object] = {}
        for lot_id, item_id, name, set_name, number in self._rows:
            if self.isInterruptionRequested():
                break
            # Isolated per row, not per loop. run()'s outer handler used to be
            # the only one, so a single row that threw — a bad URL, an unreadable
            # cache file, one item's history — abandoned the loop and published
            # whatever had accumulated so far. Every later lot then had no entry
            # in `images`, and the grid drew "No preview" for it while the art
            # sat on disk (ticket 67).
            try:
                hit = cardimages.art_for(conn, name, set_name, number)
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
            except Exception:
                continue  # this row goes without art; the rest do not
        return images, history, trends

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
                images, history, trends = self.warm_rows(
                    conn,
                    PriceSnapshotRepository(conn),
                    TrendService(conn, hist_conn),
                    date.today(),
                )
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
        self._tokens: list[str] = []
        self._intent = "All"
        self._sold = "All"
        self._in_stock_only = False
        self._review_only = False
        self._graded = "All"  # All | Singles | Graded

    def set_text(self, text: str) -> None:
        # Every whitespace-separated token must appear somewhere in the row's
        # name/set/number/SKU, so "pikachu 25/102" finds a row whose name and
        # number live in different fields (ticket 76 — voice search emits
        # exactly that shape, and typed search gains the same reach).
        self._tokens = text.lower().split()
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
        if self._tokens:
            haystack = " ".join(
                filter(
                    None,
                    [row.item.name, row.item.set_name, row.item.card_number, row.sku],
                )
            ).lower()
            if any(token not in haystack for token in self._tokens):
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
        self._parked: list = []  # workers stopping but not yet exited
        self._columns_sized = False  # auto-size grid columns once, on first load
        self._model = InventoryTableModel(
            on_edit_price=self._set_manual_price,
            on_edit_intent=self._set_lot_intent,
            on_edit_item=self._set_item_field,
            on_edit_market_at_purchase=self._set_market_at_purchase,
        )
        self._proxy = _InventoryFilterProxy()
        self._proxy.setSourceModel(self._model)

        # -- filter bar: search + segmented Intent/Sold/Type + toggle chips.
        t = theme.active()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search name / set / number / SKU…")
        self._search.addAction(
            icons.icon("search", t.ink3, 16), QLineEdit.LeadingPosition
        )
        self._search.textChanged.connect(self._proxy.set_text)
        self._search.textChanged.connect(lambda *_: self._update_summary())

        # Voice search (ticket 76): one click, say "Pikachu 25 out of 102", and
        # the snapped query lands in the search field as if typed. The
        # vocabulary it snaps to is whatever the grid currently holds.
        self._mic = VoiceInputButton(self._voice_vocabulary)
        self._mic.recognized.connect(self._search.setText)
        mic_shortcut = QShortcut(QKeySequence("Ctrl+M"), self)
        mic_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        mic_shortcut.activated.connect(self._mic.click)
        self._mic.setToolTip(self._mic.toolTip() + "  (Ctrl+M)")

        self._intent = _Segmented(
            [
                ("All", "All"),
                ("Personal", "personal"),
                ("Resale", "resale"),
                ("Ripping", "ripping"),
            ]
        )
        self._intent.changed.connect(self._proxy.set_intent)
        self._intent.changed.connect(lambda *_: self._update_summary())

        self._sold = _Segmented(
            [
                ("All", "All"),
                ("Unsold", "unsold"),
                ("Partial", "partial"),
                ("Sold", "sold"),
                # Value is the service's word ("Missing"), label the user's.
                ("Stolen", "Missing"),
            ]
        )
        self._sold.changed.connect(self._proxy.set_sold)
        self._sold.changed.connect(self._on_sold_filter)
        self._sold.changed.connect(lambda *_: self._update_summary())

        self._graded = _Segmented(
            [("All", "All"), ("Singles", "Singles"), ("Graded", "Graded")]
        )
        self._graded.changed.connect(self._proxy.set_graded)
        self._graded.changed.connect(lambda *_: self._update_summary())

        self._in_stock = _Chip("In stock")
        self._in_stock.toggled.connect(self._proxy.set_in_stock_only)
        self._in_stock.toggled.connect(lambda *_: self._update_summary())

        self._review_only = _Chip("◍ Review")
        self._review_only.setToolTip("Show only entries flagged for review")
        self._review_only.toggled.connect(self._proxy.set_review_only)
        self._review_only.toggled.connect(lambda *_: self._update_summary())

        self._delete_btn = QPushButton("Delete entry…")
        self._delete_btn.setObjectName("danger")
        self._delete_btn.setCursor(Qt.PointingHandCursor)
        self._delete_btn.setIcon(icons.icon("trash-2", t.neg, 15))
        self._delete_btn.setToolTip(
            "Permanently delete the selected entry (for correcting a mistake)"
        )
        self._delete_btn.setStyleSheet(
            f"QPushButton#danger{{background:transparent;border:1px solid "
            f"{t.line2};border-radius:9px;color:{t.neg};padding:6px 12px;"
            f"font-weight:600;font-size:12px;}}"
            f"QPushButton#danger:hover{{border-color:{t.neg};"
            f"background:{t.surface2};}}"
        )
        self._delete_btn.clicked.connect(self._delete_selected_entry)

        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(8)
        filter_bar.addWidget(self._search, 1)
        filter_bar.addWidget(self._mic)
        filter_bar.addWidget(self._intent)
        filter_bar.addWidget(self._sold)
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
        # No stretch column — the Card cell is a fixed, snug width (its content
        # isn't wide); the freed horizontal space flows to the dossier, which
        # grows with the window (see the splitter stretch factors below).
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.doubleClicked.connect(self._open_editor)
        self._table.clicked.connect(self._edit_on_click)
        self._table.selectionModel().selectionChanged.connect(self._on_selection)
        # Hover a row to show that card in the dossier (art + details + chart).
        # On leaving the rows, the dossier falls back to the selected card, or —
        # if none is selected — the most-recently-hovered card (see
        # _restore_display / eventFilter). Art is shown from the prefetch cache.
        self._dossier_art = _DossierArt()
        self._last_hovered_row: InventoryRow | None = None
        self._table.setMouseTracking(True)
        self._table.entered.connect(self._on_row_hover)
        self._table.viewport().installEventFilter(self)  # detect pointer leave
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

        # Price-history chart with the shared hover-readout + range selector
        # (same behaviour as the Dashboard). The selected window persists across
        # cards because the RangedChartView keeps its range between setChart
        # calls. Rebuilding a QChart is heavy, so coalesce rapid hover changes.
        self._chart_view = RangedChartView(default_days=90)  # 3 Months default
        self._chart_view.setMinimumHeight(190)
        self._pending_chart_row: InventoryRow | None = None
        self._chart_timer = QTimer(self)
        self._chart_timer.setSingleShot(True)
        self._chart_timer.setInterval(110)
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
        # 60/40 split favouring the grid (left); the extra room widens the Card
        # column (see _fill_columns).
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setChildrenCollapsible(False)
        body.setSizes([1200, 800])
        self._body = body
        # Columns fill the grid pane; refit when the split is dragged.
        body.splitterMoved.connect(lambda *_: self._fill_columns())

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addLayout(filter_bar)
        layout.addWidget(self._build_summary())
        layout.addWidget(body, 1)

        self.refresh()

    def refresh(self) -> None:
        self._review_delegate.disarm()
        self._thumb_cache.clear()  # image map is about to be re-warmed
        rows = self._service.inventory_rows()
        self._model.set_rows(rows)
        if not self._columns_sized and rows:
            self._table.horizontalHeader().setMinimumSectionSize(30)
            self._columns_sized = True
        self._empty.setVisible(not rows)
        self._table.setVisible(bool(rows))
        self._fill_columns()
        self._prefetch_previews(rows)
        self._update_summary()
        self._on_selection()

    # -- external navigation (Dashboard highlights → this grid) --------------

    def select_item(self, item_id: int) -> bool:
        """Select ``item_id``'s row, scroll it into view, and show it in the
        dossier (art + facts + price chart). Returns False if the item has no
        row here at all.

        An item can hold several lots (one row each): an in-stock lot wins, so a
        partially-sold card lands on the copies still on hand. Filters that would
        hide the row are cleared first — otherwise "show me this card" would
        silently do nothing whenever, say, the In-stock chip is on and the card
        is fully sold.
        """
        candidates = [
            src
            for src in range(self._model.rowCount())
            if (row := self._model.row_at(src)) is not None and row.item.id == item_id
        ]
        if not candidates:
            return False
        candidates.sort(key=lambda src: not self._model.row_at(src).in_stock)
        target = candidates[0]

        if not self._proxy.mapFromSource(self._model.index(target, 0)).isValid():
            self._clear_filters()
        index = self._proxy.mapFromSource(self._model.index(target, 0))
        if not index.isValid():  # still hidden (shouldn't happen) — say so
            return False

        row = self._model.row_at(target)
        self._last_hovered_row = row  # hover-convention fallback shows it too
        self._table.selectRow(index.row())  # → _on_selection → _display_card
        self._table.scrollTo(index, QAbstractItemView.PositionAtCenter)
        self._table.setFocus(Qt.OtherFocusReason)
        return True

    def _on_sold_filter(self, value: str) -> None:
        """A stolen lot is by definition not on hand, so leaving the In-stock
        chip active would make the Stolen segment show an empty grid. Drop the
        chip instead of dead-ending the filter."""
        if value == "Missing" and self._in_stock.isChecked():
            self._in_stock.setChecked(False)  # → set_in_stock_only(False)

    def _voice_vocabulary(self) -> tuple[list[str], list[str], list[str]]:
        """Card names, set names and collector numbers present in the grid —
        the grammar a spoken clip is decoded against (ticket 77). Read from the
        model's rows, never from the database."""
        names: set[str] = set()
        sets: set[str] = set()
        numbers: set[str] = set()
        for i in range(self._model.rowCount()):
            item = self._model.row_at(i).item
            if item.name:
                names.add(item.name)
            if item.set_name:
                sets.add(item.set_name)
            if item.card_number:
                numbers.add(item.card_number)
        return sorted(names), sorted(sets), sorted(numbers)

    def _clear_filters(self) -> None:
        """Reset the filter bar to 'show everything' (widgets included, so the
        bar never disagrees with what the grid is filtering on)."""
        self._search.clear()
        self._in_stock.setChecked(False)
        self._review_only.setChecked(False)
        self._intent.select("All")
        self._sold.select("All")
        self._graded.select("All")

    def _col(self, header: str) -> int:
        return next(
            i
            for i in range(self._model.columnCount())
            if self._model.headerData(i, Qt.Horizontal) == header
        )

    # Fixed widths for the finance/chip/link columns; the Card (Name) column
    # absorbs whatever's left so the grid always fills its pane and the extra
    # 60/40 room widens Name.
    # Widths fit each column's header label (semibold text + the QSS 8px
    # side padding) so the names render in full; the Card (Name) column gives
    # up the difference.
    _FIXED_WIDTHS = {
        "Cond/Grade": 90,
        "Intent": 72,
        "Acquired": 76,
        "Purchase Price": 108,
        "Current Market": 110,
        "Trend": 52,
        "Unrealized G/L": 110,
        "Sold For": 72,
        "TCGplayer": 82,
        "eBay": 54,
    }

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._fill_columns()

    def _fill_columns(self) -> None:
        """Keep the finance/chip/link columns at their tuned widths and let the
        Card (Name) column absorb the rest, so the grid fills its pane with no
        dead space / scroll and the extra room widens Name."""
        if not getattr(self, "_columns_sized", False):
            return
        try:
            avail = self._table.viewport().width()
        except RuntimeError:
            return  # widget torn down mid-resize
        if avail <= 1:
            return
        used = 0
        for header, width in self._FIXED_WIDTHS.items():
            self._table.setColumnWidth(self._col(header), width)
            used += width
        self._table.setColumnWidth(self._col("Name"), max(150, avail - used))

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
        # Park the outgoing worker until it has actually stopped; overwriting
        # self._prefetch_worker while it still runs frees a live QThread, which
        # Qt turns into a hard abort rather than an exception.
        retire(self._prefetch_worker, self._parked)
        payload = [
            (r.lot_id, r.item.id, r.item.name, r.item.set_name, r.item.card_number)
            for r in rows
        ]
        worker = _PreviewPrefetchWorker(db_file, payload, self._history_since())
        worker.resolved.connect(lambda w=worker: self._on_previews_resolved(w))
        self._prefetch_worker = worker
        worker.start()

    def _on_previews_resolved(self, worker: _PreviewPrefetchWorker) -> None:
        # Only the worker we are currently waiting on may publish. A retired one
        # breaks out of its loop at the next row and then emits regardless, so
        # what it carries is partial and describes a row set that has already
        # been filtered away. It used to be dropped mid-run (taking the process
        # with it, which is the bug retire() fixes); now that it survives to
        # finish, its results have to be ignored deliberately. History guards the
        # same way, by generation.
        if worker is not self._prefetch_worker:
            return
        # Results are read from the finished worker's attributes, not signal args
        # (dicts of Python objects don't survive a cross-thread queued signal).
        self._row_image = worker.images
        self._row_history = worker.history
        self._row_trends = worker.trends
        self._model.set_trends(worker.trends)
        self._thumb_cache.clear()  # rebuild thumbs from the freshly-warmed images
        self._table.viewport().update()  # repaint Card cells with their art
        # Now that art + history are warmed, refill whatever the dossier shows.
        self._display_card(self._current_display_row())

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

    # -- dossier display routing (platform-wide hover convention) ----------

    def _current_display_row(self) -> InventoryRow | None:
        """What the dossier should show when the pointer isn't over a row: the
        selected card, or — if none is selected — the last-hovered card."""
        return self._selected_row() or self._last_hovered_row

    def _display_card(self, row: InventoryRow | None) -> None:
        """Show ``row`` in the dossier: details/art now, chart debounced."""
        self._update_dossier(row)
        self._request_chart(row)

    def _restore_display(self) -> None:
        """Pointer left the rows → fall back to selected, else last-hovered."""
        self._display_card(self._current_display_row())

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        if obj is self._table.viewport() and event.type() == QEvent.Type.Leave:
            self._restore_display()
        return super().eventFilter(obj, event)

    def _on_selection(self, *_args) -> None:
        row = self._selected_row()
        self._review_delegate.note_selected_item(row.item.id if row else None)
        self._display_card(row)

    def _on_row_hover(self, proxy_index: QModelIndex) -> None:
        row = self._model.row_at(self._proxy.mapToSource(proxy_index).row())
        if row is None:
            return
        self._last_hovered_row = row
        self._display_card(row)

    # -- price-history chart ----------------------------------------------

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

    def _show_chart_message(self, text: str) -> None:
        chart = QChart()
        chart.legend().hide()
        chart.setBackgroundVisible(False)
        chart.setTitle(text)
        chart.setTitleBrush(QColor(theme.active().ink3))
        self._chart_view.setChart(chart)

    def _open_link(self, proxy_index: QModelIndex) -> None:
        """Open a card's TCGplayer/eBay listing when its link cell is clicked."""
        if proxy_index.column() not in (TCGPLAYER_COL, EBAY_COL):
            return
        url = proxy_index.data(LINK_URL_ROLE)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _show_row_menu(self, pos) -> None:
        """Right-click menu on an inventory row: stolen flag, separate a copy."""
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
        separate = menu.addAction("Separate this copy…")
        separate.setToolTip(
            "This copy is a different printing from the others under this card"
        )
        separate.setEnabled(row.sold_status == "unsold" and not row.missing)
        separate.triggered.connect(lambda: self._separate_copy(row))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _separate_copy(self, row: InventoryRow) -> None:
        """Move this lot onto its own item — "this one is the metal card".

        The grid is one row per lot over a shared item, so an item-level field
        (Notes, Variant, market value) is common to every copy. When two copies
        are actually different printings, the fix is a different item, not a
        different note (ticket 52).
        """
        variant, ok = QInputDialog.getText(
            self,
            "Separate this copy",
            f"Which printing is this copy of {row.item.name}?\n"
            "It becomes its own item, keeping its cost basis and history.",
            text=row.item.variant or "",
        )
        if not ok:
            return
        try:
            TransactionService(self._conn).split_lot_to_new_item(
                row.lot_id, variant=variant.strip() or None
            )
        except ServiceError as exc:
            QMessageBox.warning(self, "Cannot separate this copy", str(exc))
            return
        self.refresh()
        self.changed.emit()

    def _toggle_missing(self, row: InventoryRow) -> None:
        """Flag/unflag the row's lot as stolen — a reversible $0 write-down."""
        if not self._service.set_lot_missing(row.lot_id, not row.missing):
            return
        self._conn.commit()
        self.refresh()
        self.changed.emit()  # portfolio totals moved → refresh dashboard

    def _open_editor(self, proxy_index: QModelIndex) -> None:
        """Double-click → the row editor: every editable field on one screen.

        ``lot_id`` goes along with the item id, because a grid row is a *lot* and
        the editor's "This copy" section edits that lot's own fields (intent,
        acquisition snapshot, source, notes, stolen flag).
        """
        source_index = self._proxy.mapToSource(proxy_index)
        row = self._model.row_at(source_index.row())
        # The Card (Name) cell and any read-only column open the full item
        # editor; the other inline-editable cells edit in place.
        name_col = self._col("Name")
        editable = bool(self._model.flags(source_index) & Qt.ItemIsEditable)
        if editable and source_index.column() != name_col:
            return
        ItemEditorDialog(self._conn, row.item.id, lot_id=row.lot_id, parent=self).exec()
        self.refresh()  # reflect any edits made in the editor
        self.changed.emit()  # intent / missing / market moved → other screens

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
        """Inline-edit handler: change a single lot's intent (per-lot, Spec 02).

        Goes through the service so the change is audited. This used to call
        ``LotRepository.update()`` directly from the view, which wrote no
        ``audit_log`` row at all — while ``set_lot_missing`` audits for the same
        reason: intent is the personal/business split, so changing it changes
        what the lot means. (Ticket 60.)
        """
        if self._service.set_lot_intent(lot_id, Intent(intent_str)):
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

    # (key, label). "return" and "sold" are conditionally coloured / shown.
    _FACTS = [
        ("sku", "SKU"),
        ("acquired", "Acquired"),
        ("cost", "Cost basis"),
        ("market", "Market"),
        ("unrealized", "Unrealized"),
        ("return", "Return"),
        ("sold", "Sold for"),
    ]

    def _build_dossier(self) -> QWidget:
        t = theme.active()
        panel = QFrame()
        panel.setObjectName("dossier")
        panel.setStyleSheet(
            f"QFrame#dossier{{background:{t.surface};border:1px solid {t.line};"
            "border-radius:14px;}"
        )
        panel.setMinimumWidth(460)
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        cap = QLabel("SELECTED CARD")
        cap.setStyleSheet(
            f"color:{t.ink3};font-size:10px;font-weight:700;letter-spacing:2px;"
        )
        v.addWidget(cap)

        # Top region: large card art on the LEFT (fills the height), the details
        # stacked on the RIGHT. The graph gets the whole width below.
        self._dossier_name = QLabel("—")
        self._dossier_name.setStyleSheet(
            f"color:{t.ink};font-size:16px;font-weight:700;"
        )
        self._dossier_name.setWordWrap(True)

        self._dossier_sub = QLabel("")
        self._dossier_sub.setFont(self._mono(10.5))
        self._dossier_sub.setStyleSheet(f"color:{t.ink3};")
        self._dossier_sub.setWordWrap(True)

        # Facts — a fixed 2-column grid of key-over-value blocks. SKU gets its
        # own full-width row so a long SKU never widens a column and shifts the
        # others; the remaining (short) values keep the two columns at an even,
        # content-independent split.
        facts = QGridLayout()
        facts.setContentsMargins(0, 6, 0, 2)
        facts.setHorizontalSpacing(16)
        facts.setVerticalSpacing(11)
        facts.setColumnStretch(0, 1)
        facts.setColumnStretch(1, 1)
        self._fact_vals: dict[str, QLabel] = {}
        self._fact_keys: dict[str, QLabel] = {}  # the caption labels, per fact

        def make_cell(key: str, label: str, wrap: bool = False) -> QVBoxLayout:
            cell = QVBoxLayout()
            cell.setSpacing(1)
            k = QLabel(label.upper())
            k.setStyleSheet(
                f"color:{t.ink3};font-size:10px;font-weight:700;letter-spacing:.4px;"
            )
            val = QLabel("—")
            val.setFont(self._mono(13, bold=True))
            val.setStyleSheet(f"color:{t.ink};")
            if wrap:
                val.setWordWrap(True)
            cell.addWidget(k)
            cell.addWidget(val)
            self._fact_vals[key] = val
            self._fact_keys[key] = k
            return cell

        sku_label = next(lbl for key, lbl in self._FACTS if key == "sku")
        facts.addLayout(make_cell("sku", sku_label, wrap=True), 0, 0, 1, 2)
        rest = [(key, lbl) for key, lbl in self._FACTS if key != "sku"]
        for i, (key, label) in enumerate(rest):
            facts.addLayout(make_cell(key, label), 1 + i // 2, i % 2)

        details = QVBoxLayout()
        details.setSpacing(5)
        details.addWidget(self._dossier_name)
        details.addWidget(self._dossier_sub)
        details.addLayout(facts)
        details.addStretch(1)

        top_row = QHBoxLayout()
        top_row.setSpacing(16)
        top_row.addWidget(self._dossier_art, 5)  # big art on the left
        top_row.addLayout(details, 6)
        v.addLayout(top_row, 3)

        # Price-history section: divider + heading + the ranged hover-chart,
        # given the whole width and generous height below the art/details.
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background:{t.line};")
        v.addWidget(divider)
        title = QLabel("MARKET PRICE")
        title.setStyleSheet(
            f"color:{t.ink3};font-size:10px;font-weight:700;letter-spacing:1.2px;"
        )
        v.addWidget(title)
        v.addWidget(self._chart_view, 2)
        return panel

    def _update_dossier(self, row: InventoryRow | None) -> None:
        if not hasattr(self, "_fact_vals"):
            return
        t = theme.active()
        if row is None:
            self._dossier_name.setText("—")
            self._dossier_sub.setText("Select a card to see its details.")
            self._dossier_art.set_pixmap(None)
            for val in self._fact_vals.values():
                val.setText("—")
                val.setStyleSheet(f"color:{t.ink3};")
            self._show_sold_fact(False)  # no Sold-for label until relevant
            return
        self._dossier_name.setText(row.item.name)
        self._dossier_sub.setText(subline_for(row))
        # Show the card art from the prefetch cache (warmed on load) — no fetch
        # thread is spawned here, and the art fills a fixed card-aspect region.
        self._dossier_art.set_pixmap(self._art_pixmap(row.lot_id))

        def set_val(key: str, text: str, color: str) -> None:
            val = self._fact_vals[key]
            val.setText(text)
            val.setStyleSheet(f"color:{color};")
            val.setVisible(True)

        set_val("sku", row.sku or "—", t.ink2)
        set_val("acquired", row.acquired_at or "—", t.ink2)
        set_val("cost", money(row.purchase_price_cents), t.ink)
        cur = row.current_price_cents
        set_val("market", money(cur) if cur is not None else "—", t.ink)

        unreal = row.unrealized_cents
        if unreal is None:
            set_val("unrealized", "—", t.ink3)
            set_val("return", "—", t.ink3)
        else:
            sign = "+" if unreal >= 0 else "−"
            color = t.pos if unreal >= 0 else t.neg
            set_val("unrealized", f"{sign}{money(abs(unreal))}", color)
            cost = row.purchase_price_cents
            pct = (unreal / cost * 100) if cost else 0.0
            set_val("return", f"{pct:+.0f}%", color)

        # Sold-for (label + value) only appears once some of the lot has sold.
        if row.sold_price_cents is not None:
            set_val("sold", money(row.sold_price_cents), t.ink2)
            self._show_sold_fact(True)
        else:
            self._show_sold_fact(False)

    def _show_sold_fact(self, show: bool) -> None:
        """Show/hide the whole Sold-for fact — both its caption and its value."""
        self._fact_keys["sold"].setVisible(show)
        self._fact_vals["sold"].setVisible(show)

    # -- row thumbnails ---------------------------------------------------

    def _art_pixmap(self, lot_id: int) -> QPixmap | None:
        """The full cached card image for the dossier art (None until the
        background prefetch has warmed this lot's image to disk)."""
        res = self._row_image.get(lot_id)
        if not res:
            return None
        path = cardimages.cache_path(res[0])
        if not path.exists():
            return None
        pm = QPixmap(str(path))
        return None if pm.isNull() else pm

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


def _history_chart(row: InventoryRow, snapshots: list) -> QChart:
    """A themed Ember price-history chart for the dossier: an accent market line
    over an area-fill gradient, plus a dashed cost reference line. Rendered by
    the shared HoverChartView, so it carries the cursor readout + range bar. The
    RangedChartView windows the visible range (persisted across cards)."""
    t = theme.active()
    chart = QChart()
    chart.setBackgroundVisible(False)
    chart.setMargins(QMargins(0, 0, 0, 0))
    if not snapshots:
        chart.legend().hide()
        chart.setTitle("No price history yet — use File ▸ Refresh prices.")
        chart.setTitleBrush(QColor(t.ink3))
        return chart

    xs = [_to_msecs(s.as_of) for s in snapshots]
    ys = [s.price_cents / 100.0 for s in snapshots]

    # Market line (accent) — tracked by the hover readout.
    market = QLineSeries()
    market.setName("Market")
    for x, y in zip(xs, ys, strict=False):
        market.append(x, y)
    market.setPen(QPen(QColor(t.accent), 2.2))

    # Area fill under the market line: a duplicate (pen-less) line drives a
    # gradient-filled QAreaSeries, so the visible accent line isn't drawn twice.
    fill_line = QLineSeries()
    for x, y in zip(xs, ys, strict=False):
        fill_line.append(x, y)
    area = QAreaSeries(fill_line)
    # QAreaSeries does NOT own its upper series; parent it to the chart so it
    # isn't garbage-collected out from under the (dangling) C++ pointer.
    fill_line.setParent(chart)
    grad = QLinearGradient(0, 0, 0, 1)
    grad.setCoordinateMode(QGradient.CoordinateMode.ObjectBoundingMode)
    top = QColor(t.accent)
    top.setAlpha(70)
    bot = QColor(t.accent)
    bot.setAlpha(0)
    grad.setColorAt(0.0, top)
    grad.setColorAt(1.0, bot)
    area.setBrush(QBrush(grad))
    area.setPen(Qt.NoPen)

    # Dashed cost reference across the full span.
    cost = QLineSeries()
    cost.setName("Cost")
    cost_pen = QPen(QColor(t.ink3), 1.3)
    cost_pen.setStyle(Qt.DashLine)
    cost.setPen(cost_pen)
    cost.append(min(xs), row.purchase_price_cents / 100.0)
    cost.append(max(xs), row.purchase_price_cents / 100.0)

    chart.addSeries(area)
    chart.addSeries(market)
    chart.addSeries(cost)

    labels = QFont(theme.mono_family())
    labels.setPointSizeF(8.5)
    axis_x = QDateTimeAxis()
    axis_x.setFormat("MMM d")
    axis_x.setTickCount(4)
    axis_x.setLabelsFont(labels)
    axis_x.setLabelsColor(QColor(t.ink3))
    axis_x.setGridLineColor(QColor(t.line))
    axis_x.setLinePenColor(QColor(t.line))
    chart.addAxis(axis_x, Qt.AlignBottom)
    axis_y = QValueAxis()
    axis_y.setLabelFormat("$%.0f")
    axis_y.setTickCount(4)
    axis_y.setLabelsFont(labels)
    axis_y.setLabelsColor(QColor(t.ink3))
    axis_y.setGridLineColor(QColor(t.line))
    axis_y.setLinePenColor(QColor(t.line))
    chart.addAxis(axis_y, Qt.AlignLeft)
    for series in (area, market, cost):
        series.attachAxis(axis_x)
        series.attachAxis(axis_y)

    legend = chart.legend()
    legend.setVisible(True)
    legend.setAlignment(Qt.AlignTop)
    legend.setLabelColor(QColor(t.ink3))
    legend.setFont(labels)
    legend.markers(area)[0].setVisible(False)  # hide the fill from the legend
    return chart
