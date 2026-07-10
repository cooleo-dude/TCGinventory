"""Application entry point: wire up config, logging, and launch the GUI."""

from __future__ import annotations

import logging
import os
import sys

from tcginventory import config


def main() -> int:
    """Launch the TCGinventory desktop app. Returns the Qt exit code."""
    config.configure_logging()
    log = logging.getLogger(__name__)

    data_dir = config.ensure_data_dir()
    log.info("Data directory: %s", data_dir)
    log.info("Database path: %s", config.db_path())

    # Open the DB and run any pending migrations on startup (F-DA-7). The
    # connection stays open for the app's lifetime; the views read from it.
    from tcginventory.data.db import open_database
    from tcginventory.data.migrator import current_version

    conn = open_database(config.db_path())
    log.info("Schema version: %d", current_version(conn))

    # Assign SKUs to any pre-existing lots that predate the SKU system.
    from tcginventory.services.sku import backfill_skus

    filled = backfill_skus(conn)
    if filled:
        log.info("Backfilled %d lot SKU(s).", filled)

    # Optional demo seeding for a fresh DB (dev/verification only).
    if os.environ.get("TCGINVENTORY_SEED") == "1":
        empty = conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 0
        if empty:
            from tcginventory.data.seed import seed_demo

            seed_demo(conn)
            log.info("Seeded demo data.")

    # Automatic timestamped backup on startup (F-DA-6); never blocks launch.
    if conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] > 0:
        try:
            from tcginventory.data.backup import auto_backup, timestamp_for_filename

            dest = auto_backup(
                conn, data_dir / "backups", stamp=timestamp_for_filename()
            )
            log.info("Startup backup: %s", dest)
        except Exception:
            log.exception("Startup backup failed (continuing)")

    # Imported lazily so headless tooling (tests, tools) can import this module
    # without requiring a Qt display or the PySide6 dependency at import time.
    from PySide6.QtWidgets import QApplication

    from tcginventory.ui.assets import app_icon
    from tcginventory.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setWindowIcon(app_icon())
    window = MainWindow(conn)
    window.show()
    try:
        return app.exec()
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
