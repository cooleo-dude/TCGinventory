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

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
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
from tcginventory.data.repositories import PriceSnapshotRepository
from tcginventory.services import importexport
from tcginventory.ui.assets import app_icon
from tcginventory.ui.views.dashboard import DashboardView
from tcginventory.ui.views.history import HistoryView
from tcginventory.ui.views.inventory import InventoryView
from tcginventory.ui.views.transaction import TransactionView

NAV_ITEMS = ["Dashboard", "Transaction", "Inventory", "History"]
_TRANSACTION_INDEX = 1
_REFRESH_STALE_HOURS = 12  # startup auto-refresh only if older than this


class _PriceRefreshWorker(QThread):
    """Runs a price refresh off the UI thread with its own DB connection."""

    done = Signal(object)  # RefreshSummary

    def __init__(self, db_path) -> None:
        super().__init__()
        self._db_path = db_path

    def run(self) -> None:
        import logging

        from tcginventory.data.db import open_database
        from tcginventory.services.pricing import PricingService, RefreshSummary

        try:
            conn = open_database(self._db_path)
            try:
                summary = PricingService(conn).refresh()
            finally:
                conn.close()
        except Exception as exc:  # never crash the thread
            summary = RefreshSummary(errors=[str(exc)])
        logging.getLogger(__name__).info(
            "price refresh: priced=%d unmapped=%d errors=%s",
            summary.priced,
            len(summary.unmapped),
            summary.errors,
        )
        self.done.emit(summary)


class _CatalogUpdateWorker(QThread):
    """Pulls the reference card catalog off the UI thread (its own connection)."""

    done = Signal(object)  # CatalogUpdateSummary

    def __init__(self, db_path) -> None:
        super().__init__()
        self._db_path = db_path

    def run(self) -> None:
        import logging

        from tcginventory.data.db import open_database
        from tcginventory.services.catalog import CatalogUpdateSummary, update_catalog

        try:
            conn = open_database(self._db_path)
            try:
                summary = update_catalog(conn)
            finally:
                conn.close()
        except Exception as exc:  # never crash the thread
            summary = CatalogUpdateSummary(errors=[str(exc)])
        logging.getLogger(__name__).info(
            "catalog update: sets=%d cards=%d errors=%d",
            summary.sets,
            summary.cards,
            len(summary.errors),
        )
        self.done.emit(summary)


class MainWindow(QMainWindow):
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__()
        # Standalone/tests: fall back to a fresh in-memory database.
        if conn is None:
            conn = connect(":memory:")
            migrate(conn)
        self._conn = conn
        self._db_file = self._resolve_db_file()  # None for in-memory (tests)
        self._price_worker: _PriceRefreshWorker | None = None
        self._catalog_worker: _CatalogUpdateWorker | None = None

        self.setWindowTitle(f"TCGinventory {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1280, 960)  # taller so the half-height calendar fits tall cells

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
        self._inventory.changed.connect(self._on_ledger_changed)
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

    # -- price refresh ------------------------------------------------------

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
        if not self._db_file:
            return
        has_items = self._conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] > 0
        if has_items and self._prices_are_stale():
            self._refresh_prices(silent=True)

    def _refresh_prices(self, silent: bool = False) -> None:
        if not self._db_file:
            if not silent:
                QMessageBox.information(
                    self, "Refresh prices", "Not available for an in-memory database."
                )
            return
        if self._price_worker is not None and self._price_worker.isRunning():
            return
        self._refresh_silent = silent
        self._refresh_action.setEnabled(False)
        self.statusBar().showMessage("Refreshing prices…")
        self._price_worker = _PriceRefreshWorker(self._db_file)
        self._price_worker.done.connect(self._on_prices_refreshed)
        self._price_worker.start()

    def _on_prices_refreshed(self, summary) -> None:
        self._refresh_action.setEnabled(True)
        self._price_worker = None
        self._on_ledger_changed()  # re-read views (prices now live)
        if not self._refresh_silent:
            msg = f"Priced {summary.priced} item(s)."
            if summary.unmapped:
                msg += (
                    f"\n{len(summary.unmapped)} not priced "
                    "(missing TCGplayer link or unmatched set)."
                )
            if summary.errors:
                msg += "\nErrors: " + "; ".join(summary.errors)
            QMessageBox.information(self, "Prices refreshed", msg)

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
        self._file_menu.addSeparator()
        self._refresh_action = self._file_menu.addAction(
            "Refresh prices…", lambda: self._refresh_prices(silent=False)
        )
        self._catalog_action = self._file_menu.addAction(
            "Update card database…", self._update_catalog
        )
        self._build_auto_update_action()

    def _update_catalog(self) -> None:
        if not self._db_file:
            QMessageBox.information(
                self, "Card database", "Not available for an in-memory database."
            )
            return
        if self._catalog_worker is not None and self._catalog_worker.isRunning():
            return
        self._catalog_action.setEnabled(False)
        self.statusBar().showMessage("Updating card database… (one-time bulk pull)")
        self._catalog_worker = _CatalogUpdateWorker(self._db_file)
        self._catalog_worker.done.connect(self._on_catalog_updated)
        self._catalog_worker.start()

    def _on_catalog_updated(self, summary) -> None:
        self._catalog_action.setEnabled(True)
        self._catalog_worker = None
        self._update_status()
        if summary.errors and not summary.cards:
            QMessageBox.warning(
                self, "Card database", "Update failed:\n" + "\n".join(summary.errors)
            )
        else:
            msg = f"Loaded {summary.cards:,} cards from {summary.sets} sets."
            if summary.errors:
                msg += f"\n({len(summary.errors)} set(s) skipped)"
            QMessageBox.information(self, "Card database updated", msg)

    def _build_auto_update_action(self) -> None:
        from tcginventory import scheduling

        if not scheduling.is_supported():
            return
        self._auto_update_action = self._file_menu.addAction(
            "Automatic daily price updates"
        )
        self._auto_update_action.setCheckable(True)
        self._auto_update_action.setToolTip(
            "Refresh prices once a day in the background, even when the app is "
            "closed (uses the Windows Task Scheduler)."
        )
        # Default ON: the preference is on unless the user has turned it off.
        enabled = self._auto_update_pref()
        # Set state before connecting so this doesn't fire the toggle handler.
        self._auto_update_action.setChecked(enabled)
        self._auto_update_action.toggled.connect(self._toggle_auto_updates)
        # Only the packaged app registers the OS task (so the task points at the
        # exe, and running from source / tests has no side effects).
        if getattr(sys, "frozen", False):
            self._reconcile_auto_updates(enabled)

    @staticmethod
    def _settings():
        from PySide6.QtCore import QSettings

        return QSettings(config.APP_NAME, config.APP_NAME)

    def _auto_update_pref(self) -> bool:
        return bool(self._settings().value("auto_update/enabled", True, type=bool))

    def _reconcile_auto_updates(self, enabled: bool) -> None:
        """Make the OS task match the preference (self-heals a missing task)."""
        from tcginventory import scheduling

        try:
            if enabled and not scheduling.is_enabled():
                scheduling.enable()
            elif not enabled and scheduling.is_enabled():
                scheduling.disable()
        except Exception:
            logging.getLogger(__name__).exception("auto-update reconcile failed")

    def _toggle_auto_updates(self, on: bool) -> None:
        from tcginventory import scheduling

        try:
            if on:
                scheduling.enable()
            else:
                scheduling.disable()
        except Exception as exc:
            QMessageBox.warning(self, "Automatic updates", str(exc))
            # Reflect the real state after a failure.
            self._auto_update_action.blockSignals(True)
            self._auto_update_action.setChecked(scheduling.is_enabled())
            self._auto_update_action.blockSignals(False)
            return
        self._settings().setValue("auto_update/enabled", on)
        if on:
            QMessageBox.information(
                self,
                "Automatic updates on",
                "Prices will refresh once a day in the background (around "
                f"{scheduling.DEFAULT_TIME}), even when the app is closed.",
            )

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
