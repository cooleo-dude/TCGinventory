"""The application's main window.

Hosts the four navigation destinations over a shared database connection:
Dashboard, the unified Transaction window (write UI), Inventory, and History.
Posting/void/edit refresh the read views so everything stays in sync.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from tcginventory import __version__, config
from tcginventory.data.auctions import open_auctions
from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.data.repositories import PriceSnapshotRepository
from tcginventory.ui.assets import app_icon, scrafty_movie_path
from tcginventory.ui.views.auctions import AuctionsView
from tcginventory.ui.views.calendar import CalendarView
from tcginventory.ui.views.dashboard import DashboardView
from tcginventory.ui.views.history import HistoryView
from tcginventory.ui.views.inventory import InventoryView
from tcginventory.ui.views.market import PriceLookupView
from tcginventory.ui.views.market_signal import MarketView
from tcginventory.ui.views.operations import OperationsView
from tcginventory.ui.views.recognition import RecognitionView
from tcginventory.ui.views.transaction import TransactionView

NAV_ITEMS = [
    "Dashboard",
    "Transaction",
    "Inventory",
    "History",
    "Calendar",
    "Price Lookup",
    "eBay Auctions",
    "Recognition",
    "Market",
    "Operations",
]
# Lucide icon per nav destination (see ui/icons.py).
NAV_ICONS = {
    "Dashboard": "layout-dashboard",
    "Transaction": "arrow-left-right",
    "Inventory": "package",
    "History": "scroll-text",
    "Calendar": "calendar-days",
    "Price Lookup": "search",
    "eBay Auctions": "gavel",
    "Recognition": "scan-line",
    "Market": "trending-up",
    "Operations": "sliders-horizontal",
}
# Derived, not hardcoded: inserting a destination silently invalidated literals.
_TRANSACTION_INDEX = NAV_ITEMS.index("Transaction")
_INVENTORY_INDEX = NAV_ITEMS.index("Inventory")
_CALENDAR_INDEX = NAV_ITEMS.index("Calendar")
_AUCTION_INDEX = NAV_ITEMS.index("eBay Auctions")
_OPERATIONS_INDEX = NAV_ITEMS.index("Operations")
_REFRESH_STALE_HOURS = 12  # startup auto-refresh only if older than this


class _MascotLabel(QLabel):
    """Displays a ``QMovie`` scaled to the label's current width (aspect kept), so
    the mascot fills its column instead of leaving empty space beside it — and
    tracks the width if the nav splitter is dragged."""

    def __init__(self, movie: QMovie, aspect: float) -> None:
        super().__init__()
        self._movie = movie
        self._aspect = aspect  # native height / width
        self._scaled_w = 0
        self.setMovie(movie)
        self._scale_to(200)  # sensible default before the first layout pass

    def _scale_to(self, width: int) -> None:
        self._scaled_w = width
        self._movie.setScaledSize(QSize(width, round(width * self._aspect)))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = self.width()
        if width > 0 and width != self._scaled_w:
            self._scale_to(width)


class MainWindow(QMainWindow):
    def __init__(self, conn: sqlite3.Connection | None = None, progress=None) -> None:
        super().__init__()
        # Standalone/tests: fall back to a fresh in-memory database.
        if conn is None:
            conn = connect(":memory:")
            migrate(conn)
        self._conn = conn
        self._db_file = self._resolve_db_file()  # None for in-memory (tests)

        self.setWindowTitle(f"TCGinventory {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1280, 960)  # taller so the half-height calendar fits tall cells

        # Building the ten views is the two seconds of startup, spread fairly
        # evenly across them. ``progress(name)`` is called before each one so the
        # splash can name it and repaint; nothing here changes without it.
        def announce(name: str) -> None:
            if progress is not None:
                progress(name)

        # Watched auctions live in their own file; mirror the main DB's storage
        # nature so an in-memory app (tests) doesn't touch the real auctions.db.
        builders = (
            ("Dashboard", lambda: DashboardView(conn)),
            ("Transaction", lambda: TransactionView(conn)),
            ("Inventory", lambda: InventoryView(conn)),
            ("History", lambda: HistoryView(conn)),
            ("Calendar", lambda: CalendarView(conn)),
            ("Price Lookup", lambda: PriceLookupView(conn)),
            (
                "eBay Auctions",
                lambda: AuctionsView(
                    conn,
                    auctions_conn=open_auctions(None if self._db_file else ":memory:"),
                ),
            ),
            ("Recognition", lambda: RecognitionView(conn, prewarm=True)),
            ("Market", lambda: MarketView(conn)),
            ("Operations", lambda: OperationsView(conn)),
        )
        # Built (and stacked) in NAV_ITEMS order — the nav indexes into the stack.
        self._stack = QStackedWidget()
        views: dict[str, QWidget] = {}
        for name, build in builders:
            announce(name)
            view = build()
            views[name] = view
            self._stack.addWidget(view)

        self._dashboard = views["Dashboard"]
        self._transaction = views["Transaction"]
        self._inventory = views["Inventory"]
        self._history = views["History"]
        self._calendar = views["Calendar"]
        self._price_lookup = views["Price Lookup"]
        self._auctions = views["eBay Auctions"]
        self._recognition = views["Recognition"]
        self._market = views["Market"]
        self._operations = views["Operations"]

        # Refresh-on-demand: every view is pre-built and refreshed once on first
        # visit; a screen only re-refreshes when the data behind it has actually
        # changed. `_data_version` bumps on every mutation source (ledger edits,
        # imports, background price/catalog sweeps); each view records the version
        # it last refreshed at, so bouncing between unchanged screens is instant.
        self._data_version = 0
        self._view_versions: dict[int, int] = {}

        # Keep read views current as the ledger changes.
        self._transaction.posted.connect(self._on_ledger_changed)
        self._history.changed.connect(self._on_ledger_changed)
        self._inventory.changed.connect(self._on_ledger_changed)
        # An import or a price refresh changes what every read screen shows.
        self._operations.changed.connect(self._on_ledger_changed)
        self._history.edit_requested.connect(self._start_edit)
        self._history.new_transaction_requested.connect(
            lambda: self._nav.setCurrentRow(_TRANSACTION_INDEX)
        )
        # Dashboard highlights are links into the screen that owns the detail.
        self._dashboard.item_activated.connect(self._show_item_in_inventory)
        self._dashboard.show_date_activated.connect(self._show_date_in_calendar)
        self._auctions.export_to_transaction.connect(self._export_from_auction)
        self._recognition.export_to_transaction.connect(
            self._export_recognition_to_transaction
        )
        self._recognition.export_to_auction.connect(self._export_recognition_to_auction)
        # Recognition History re-exporting a stored capture takes the same route.
        self._history.export_to_transaction.connect(
            self._export_recognition_to_transaction
        )
        self._history.export_to_auction.connect(self._export_recognition_to_auction)

        self._nav = QListWidget()
        self._nav.setObjectName("nav")
        self._nav.addItems(NAV_ITEMS)
        self._nav.setMinimumWidth(168)
        self._nav.setMaximumWidth(200)
        self._nav.setIconSize(QSize(18, 18))
        self._paint_nav_icons(0)
        self._nav.currentRowChanged.connect(self._on_nav_changed)

        # Nav panel: the nav list, with the Scrafty mascot filling the bottom of
        # the column and animating at all times. The mascot fills the column width
        # (aspect-preserved) so there's no empty bar beside it.
        self._scrafty_movie = QMovie(scrafty_movie_path())
        self._scrafty_movie.jumpToFrame(0)
        native = self._scrafty_movie.currentPixmap().size()
        aspect = native.height() / native.width() if native.width() else 1.0
        self._scrafty = _MascotLabel(self._scrafty_movie, aspect)
        self._scrafty.setObjectName("scrafty")
        self._scrafty.setAlignment(Qt.AlignBottom)
        self._scrafty_movie.start()

        nav_panel = QWidget()
        nav_panel.setMaximumWidth(200)  # keep the column the nav's width
        nav_col = QVBoxLayout(nav_panel)
        nav_col.setContentsMargins(0, 0, 0, 0)
        nav_col.setSpacing(0)
        nav_col.addWidget(self._nav, 1)
        nav_col.addWidget(self._scrafty, 0)  # no alignment → fills the column width

        splitter = QSplitter()
        splitter.addWidget(nav_panel)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(1, 1)

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.setCentralWidget(container)

        self._build_menu()
        self._reconcile_auto_updates()
        self.setStatusBar(QStatusBar())
        # Persistent (right-aligned) indicator of when prices last updated —
        # stays visible even while a transient "Refreshing…" message shows.
        self._last_refresh_label = QLabel()
        self._last_refresh_label.setToolTip(
            "When market prices were last recorded (GUI, manual, or the daily "
            "background update)."
        )
        self.statusBar().addPermanentWidget(self._last_refresh_label)
        self._update_status()

        self._nav.setCurrentRow(0)
        # Throttled auto-refresh shortly after the window shows (file DB only).
        QTimer.singleShot(0, self._maybe_startup_refresh)

    # -- startup upkeep ------------------------------------------------------

    def _resolve_db_file(self) -> str | None:
        for _seq, name, file in self._conn.execute("PRAGMA database_list").fetchall():
            if name == "main":
                return file or None  # empty string for :memory:
        return None

    def _prices_are_stale(self) -> bool:
        row = self._conn.execute("SELECT MAX(as_of) FROM price_snapshot").fetchone()
        last = row[0]
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return datetime.now(timezone.utc) - last_dt > timedelta(
            hours=_REFRESH_STALE_HOURS
        )

    def _maybe_startup_refresh(self) -> None:
        """Throttled auto-refresh shortly after launch (file DB only).

        Runs *through the Operations screen* so its tile, its Activity entry and
        the status bar all say the same thing as a manual run.
        """
        if not self._db_file:
            return
        has_items = self._conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] > 0
        if has_items and self._prices_are_stale():
            self._operations.refresh_prices(silent=True)

    # -- menu ------------------------------------------------------------------

    def _build_menu(self) -> None:
        """A deliberately thin File menu.

        Every maintenance verb lives on the **Operations** screen, which can say
        what each one does and when it last ran — a menu label can do neither, and
        two trigger paths for one job drift apart.
        """
        # Keep a reference so the QMenu isn't garbage-collected (PySide6 quirk).
        self._file_menu = self.menuBar().addMenu("&File")
        self._operations_action = self._file_menu.addAction(
            "Operations", lambda: self._nav.setCurrentRow(_OPERATIONS_INDEX)
        )
        self._operations_action.setShortcut("Ctrl+Shift+O")
        self._operations_action.setStatusTip(
            "Imports, exports, backups, price and card-database updates"
        )
        self._file_menu.addSeparator()
        self._file_menu.addAction("Exit", self.close)

    # -- automatic daily updates (the OS task; the toggle lives in Operations) --

    @staticmethod
    def _settings():
        from PySide6.QtCore import QSettings

        return QSettings(config.APP_NAME, config.APP_NAME)

    def _auto_update_pref(self) -> bool:
        return bool(self._settings().value("auto_update/enabled", True, type=bool))

    def _reconcile_auto_updates(self) -> None:
        """Make the scheduled task match the preference, self-healing one that
        went missing. Only the packaged app registers it, so running from source
        (or under tests) has no side effects.
        """
        from tcginventory import scheduling

        if not getattr(sys, "frozen", False) or not scheduling.is_supported():
            return
        enabled = self._auto_update_pref()
        try:
            if enabled and not scheduling.is_enabled():
                scheduling.enable()
            elif not enabled and scheduling.is_enabled():
                scheduling.disable()
        except Exception:
            logging.getLogger(__name__).exception("auto-update reconcile failed")

    def changeEvent(self, event) -> None:
        """Pause the Scrafty mascot while the window isn't focused; resume on
        activation, so it only animates when the app is in front."""
        if event.type() == QEvent.Type.ActivationChange and hasattr(
            self, "_scrafty_movie"
        ):
            self._scrafty_movie.setPaused(not self.isActiveWindow())
        super().changeEvent(event)

    def _paint_nav_icons(self, current: int) -> None:
        """Tint each nav icon — accent for the active row, muted for the rest."""
        from tcginventory.ui import icons, theme

        t = theme.active()
        for i in range(self._nav.count()):
            name = NAV_ITEMS[i]
            color = t.accent if i == current else t.ink2
            self._nav.item(i).setIcon(icons.icon(NAV_ICONS[name], color, 18))

    def _on_nav_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._paint_nav_icons(index)
        # Only pay the refresh cost when this view is stale (never shown, or the
        # data changed since it was last shown). Unchanged screens re-show instantly.
        if self._view_versions.get(index) != self._data_version:
            widget = self._stack.widget(index)
            refresh = getattr(widget, "refresh", None)
            if callable(refresh):
                refresh()
            self._view_versions[index] = self._data_version
        self._update_status()

    def _refresh_current(self) -> None:
        """Refresh only the visible view (so it updates live) and mark it fresh;
        other views pick up the change lazily on their next visit."""
        index = self._stack.currentIndex()
        widget = self._stack.widget(index)
        refresh = getattr(widget, "refresh", None)
        if callable(refresh):
            refresh()
        self._view_versions[index] = self._data_version

    def _invalidate_views(self) -> None:
        """Mark all views stale after a data mutation and refresh the visible one."""
        self._data_version += 1
        self._refresh_current()

    def _on_ledger_changed(self) -> None:
        self._invalidate_views()
        self._update_status()

    def _start_edit(self, transaction_id: int) -> None:
        if self._transaction.load_transaction(transaction_id):
            self._nav.setCurrentRow(_TRANSACTION_INDEX)

    def _show_item_in_inventory(self, item_id: int) -> None:
        """Dashboard mover/seller → that card selected in Inventory.

        Navigate first: switching screens can refresh the grid (which rebuilds
        its rows and drops any selection), so the selection has to come after.
        """
        self._nav.setCurrentRow(_INVENTORY_INDEX)
        if not self._inventory.select_item(item_id):
            self.statusBar().showMessage(
                "That card is no longer in the inventory grid.", 5000
            )

    def _show_date_in_calendar(self, iso: str) -> None:
        """Dashboard upcoming show → that day on the Calendar."""
        self._nav.setCurrentRow(_CALENDAR_INDEX)
        self._calendar.show_date(iso)

    def _export_from_auction(self, payload) -> None:
        """Auction → Transaction: prefill a fresh purchase with the auction's
        cards (you enter the winning bid as cash out)."""
        title, incoming = payload
        self._transaction.clear()
        for card in incoming:
            self._transaction.add_incoming(card)
        self._transaction.set_deal_context(counterparty="eBay", reference=title)
        self._nav.setCurrentRow(_TRANSACTION_INDEX)

    def _export_recognition_to_transaction(self, cards) -> None:
        """Recognition (Transaction session) → Transaction window."""
        if self._transaction.has_content() and not self._confirm_overwrite(
            "the current transaction"
        ):
            return
        unmatched = self._transaction.import_recognized(cards)
        self._nav.setCurrentRow(_TRANSACTION_INDEX)
        if unmatched:
            QMessageBox.information(
                self,
                "Some cards you're giving aren't in inventory",
                "These give-side cards have no matching sellable lot, so they "
                "were skipped — add them to inventory first:\n\n• "
                + "\n• ".join(unmatched),
            )

    def _export_recognition_to_auction(self, cards) -> None:
        """Recognition (Listing session) → Auction window."""
        if self._auctions.editor_has_content() and not self._confirm_overwrite(
            "the auction you're planning"
        ):
            return
        self._auctions.import_recognized(cards)
        self._nav.setCurrentRow(_AUCTION_INDEX)

    def _confirm_overwrite(self, what: str) -> bool:
        return (
            QMessageBox.question(
                self,
                "Replace current cards?",
                f"This will clear {what} and fill it with the recognized cards. "
                "Continue?",
            )
            == QMessageBox.Yes
        )

    def _update_status(self) -> None:
        count = self._conn.execute("SELECT COUNT(*) FROM item").fetchone()[0]
        self.statusBar().showMessage(f"DB: {config.db_path()}   ·   {count} items")
        self._last_refresh_label.setText(
            f"Prices last updated: {self._last_refresh_text()}"
        )

    def _last_refresh_text(self) -> str:
        """Full local date + time of the most recent price snapshot, or 'never'."""
        as_of = PriceSnapshotRepository(self._conn).latest_as_of()
        if not as_of:
            return "never"
        try:
            dt = datetime.fromisoformat(as_of)
        except ValueError:
            return as_of  # show the raw value rather than nothing
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # UTC snapshot → the user's local time
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
