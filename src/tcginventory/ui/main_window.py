"""The application's main window.

Hosts the four navigation destinations over a shared database connection:
Dashboard, the unified Transaction window (write UI), Inventory, and History.
Posting/void/edit refresh the read views so everything stays in sync.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from tcginventory import __version__, config
from tcginventory.data.backup import backup_database
from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.services import importexport
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

        self._build_menu()
        self.setStatusBar(QStatusBar())
        self._update_status()

        self._nav.setCurrentRow(0)

    # -- menu (import / export / backup) ------------------------------------

    def _build_menu(self) -> None:
        # Keep a reference so the QMenu isn't garbage-collected (PySide6 quirk).
        self._file_menu = self.menuBar().addMenu("&File")
        self._file_menu.addAction("Import items from CSV…", self._import_items)
        self._file_menu.addAction("Import purchases from CSV…", self._import_purchases)
        self._file_menu.addSeparator()
        self._file_menu.addAction("Export inventory to CSV…", self._export_inventory)
        self._file_menu.addAction("Export full backup (CSV folder)…", self._export_all)
        self._file_menu.addAction("Backup database…", self._backup_database)

    def _backup_database(self) -> None:
        default = str(config.data_dir() / "inventory-backup.db")
        path, _ = QFileDialog.getSaveFileName(
            self, "Backup database", default, "SQLite database (*.db)"
        )
        if not path:
            return
        try:
            backup_database(self._conn, path)
        except Exception as exc:  # pragma: no cover - surfaced to user
            QMessageBox.warning(self, "Backup failed", str(exc))
            return
        QMessageBox.information(self, "Backup complete", f"Saved to:\n{path}")

    def _export_inventory(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export inventory", "inventory.csv", "CSV (*.csv)"
        )
        if not path:
            return
        importexport.export_inventory_csv(self._conn, path)
        QMessageBox.information(self, "Export complete", f"Saved to:\n{path}")

    def _export_all(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Export full backup folder")
        if not directory:
            return
        importexport.export_all(self._conn, directory)
        importexport.export_items_csv(self._conn, f"{directory}/items_readable.csv")
        importexport.export_inventory_csv(
            self._conn, f"{directory}/inventory_readable.csv"
        )
        importexport.export_transactions_csv(
            self._conn, f"{directory}/transactions_readable.csv"
        )
        QMessageBox.information(self, "Export complete", f"Wrote CSVs to:\n{directory}")

    def _import_items(self) -> None:
        self._run_import("items", importexport.import_items, "items")

    def _import_purchases(self) -> None:
        self._run_import("purchases", importexport.import_purchases, "purchases")

    def _run_import(self, mode: str, apply_fn, label: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, f"Import {label} from CSV", "", "CSV (*.csv)"
        )
        if not path:
            return
        preview = importexport.preview_import(path, mode=mode)
        summary = f"{preview.ok_count} valid rows, {preview.error_count} error(s)."
        if preview.errors:
            summary += "\n\n" + "\n".join(preview.errors[:10])
            if preview.error_count > 10:
                summary += f"\n…and {preview.error_count - 10} more."
        if preview.ok_count == 0:
            QMessageBox.warning(self, "Nothing to import", summary)
            return
        prompt = f"{summary}\n\nImport the {preview.ok_count} valid rows?"
        if QMessageBox.question(self, "Confirm import", prompt) != QMessageBox.Yes:
            return
        result = apply_fn(self._conn, path)
        self._on_ledger_changed()
        done = (
            f"Created {result.created}, matched {result.matched}, "
            f"posted {result.posted}."
        )
        QMessageBox.information(self, "Import complete", done)

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
