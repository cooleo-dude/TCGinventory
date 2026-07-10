"""The application's main window.

Hosts the four navigation destinations. In M3 the Dashboard, Inventory, and
History views are live (read-only) over a real database; the Transaction entry
screen is a placeholder until M4 (write UI).
"""

from __future__ import annotations

import sqlite3

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from tcginventory import __version__, config
from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.ui.views.dashboard import DashboardView
from tcginventory.ui.views.history import HistoryView
from tcginventory.ui.views.inventory import InventoryView

NAV_ITEMS = ["Dashboard", "Transaction", "Inventory", "History"]


def _placeholder(text: str) -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    label = QLabel(text)
    label.setContentsMargins(24, 24, 24, 24)
    layout.addWidget(label)
    layout.addStretch(1)
    return widget


class MainWindow(QMainWindow):
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__()
        # Standalone/tests: fall back to a fresh in-memory database.
        if conn is None:
            conn = connect(":memory:")
            migrate(conn)
        self._conn = conn

        self.setWindowTitle(f"TCGinventory {__version__}")
        self.resize(1150, 760)

        self._dashboard = DashboardView(conn)
        self._inventory = InventoryView(conn)
        self._history = HistoryView(conn)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._dashboard)  # 0 Dashboard
        self._stack.addWidget(
            _placeholder("Transaction entry — coming in M4 (write UI).")
        )
        self._stack.addWidget(self._inventory)  # 2 Inventory
        self._stack.addWidget(self._history)  # 3 History

        nav = QListWidget()
        nav.addItems(NAV_ITEMS)
        nav.setMaximumWidth(180)
        nav.currentRowChanged.connect(self._on_nav_changed)

        splitter = QSplitter()
        splitter.addWidget(nav)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(1, 1)

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.setCentralWidget(container)

        self.setStatusBar(QStatusBar())
        self._update_status()

        nav.setCurrentRow(0)

    def _on_nav_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        # Refresh the destination so it reflects the latest data.
        widget = self._stack.widget(index)
        refresh = getattr(widget, "refresh", None)
        if callable(refresh):
            refresh()
        self._update_status()

    def _update_status(self) -> None:
        count = self._conn.execute("SELECT COUNT(*) FROM item").fetchone()[0]
        self.statusBar().showMessage(f"DB: {config.db_path()}   ·   {count} items")
