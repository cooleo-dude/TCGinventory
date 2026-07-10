"""The application's main window.

Hosts the four navigation destinations over a shared database connection:
Dashboard, the unified Transaction window (write UI), Inventory, and History.
Posting/void/edit refresh the read views so everything stays in sync.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from tcginventory import __version__, config
from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.ui.views.dashboard import DashboardView
from tcginventory.ui.views.history import HistoryView
from tcginventory.ui.views.inventory import InventoryView
from tcginventory.ui.views.transaction import TransactionView

NAV_ITEMS = ["Dashboard", "Transaction", "Inventory", "History"]
_TRANSACTION_INDEX = 1


class MainWindow(QMainWindow):
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__()
        # Standalone/tests: fall back to a fresh in-memory database.
        if conn is None:
            conn = connect(":memory:")
            migrate(conn)
        self._conn = conn

        self.setWindowTitle(f"TCGinventory {__version__}")
        self.resize(1200, 800)

        self._dashboard = DashboardView(conn)
        self._transaction = TransactionView(conn)
        self._inventory = InventoryView(conn)
        self._history = HistoryView(conn)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._dashboard)  # 0 Dashboard
        self._stack.addWidget(self._transaction)  # 1 Transaction
        self._stack.addWidget(self._inventory)  # 2 Inventory
        self._stack.addWidget(self._history)  # 3 History

        # Keep read views current as the ledger changes.
        self._transaction.posted.connect(self._on_ledger_changed)
        self._history.changed.connect(self._on_ledger_changed)
        self._history.edit_requested.connect(self._start_edit)

        self._nav = QListWidget()
        self._nav.addItems(NAV_ITEMS)
        self._nav.setMaximumWidth(180)
        self._nav.currentRowChanged.connect(self._on_nav_changed)

        splitter = QSplitter()
        splitter.addWidget(self._nav)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(1, 1)

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.setCentralWidget(container)

        self.setStatusBar(QStatusBar())
        self._update_status()

        self._nav.setCurrentRow(0)

    def _on_nav_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        widget = self._stack.widget(index)
        refresh = getattr(widget, "refresh", None)
        if callable(refresh):
            refresh()
        self._update_status()

    def _on_ledger_changed(self) -> None:
        self._dashboard.refresh()
        self._inventory.refresh()
        self._history.refresh()
        self._update_status()

    def _start_edit(self, transaction_id: int) -> None:
        if self._transaction.load_transaction(transaction_id):
            self._nav.setCurrentRow(_TRANSACTION_INDEX)

    def _update_status(self) -> None:
        count = self._conn.execute("SELECT COUNT(*) FROM item").fetchone()[0]
        self.statusBar().showMessage(f"DB: {config.db_path()}   ·   {count} items")
